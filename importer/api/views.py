#########################################################################
#
# Copyright (C) 2021 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################
import logging
from urllib.parse import urljoin
from django.conf import settings
from django.urls import reverse

from geonode.resource.enumerator import ExecutionRequestAction
from django.utils.translation import gettext_lazy as _
from dynamic_rest.filters import DynamicFilterBackend, DynamicSortingFilter
from dynamic_rest.viewsets import DynamicModelViewSet
from geonode.base.api.filters import DynamicSearchFilter, ExtentFilter, FavoriteFilter
from geonode.base.api.pagination import GeoNodeApiPagination
from geonode.base.api.permissions import (
    ResourceBasePermissionsFilter,
    UserHasPerms,
)
from geonode.base.api.serializers import ResourceBaseSerializer
from geonode.base.api.views import ResourceBaseViewSet
from geonode.base.models import ResourceBase
from geonode.storage.manager import StorageManager
from geonode.upload.api.permissions import UploadPermissionsFilter
from geonode.upload.utils import UploadLimitValidator
from importer.api.exception import HandlerException, ImportException
from importer.api.serializer import ImporterSerializer
from importer.celery_tasks import import_orchestrator
from importer.orchestrator import orchestrator
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.parsers import FileUploadParser, MultiPartParser
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class ImporterViewSet(DynamicModelViewSet):
    """
    API endpoint that allows uploads to be viewed or edited.
    """

    parser_class = [FileUploadParser, MultiPartParser]

    authentication_classes = [
        BasicAuthentication,
        SessionAuthentication,
        OAuth2Authentication,
    ]
    permission_classes = [
        IsAuthenticatedOrReadOnly,
        UserHasPerms(perms_dict={"default": {"POST": ["base.add_resourcebase"]}}),
    ]
    filter_backends = [
        DynamicFilterBackend,
        DynamicSortingFilter,
        DynamicSearchFilter,
        UploadPermissionsFilter,
    ]
    queryset = ResourceBase.objects.all().order_by("-last_updated")
    serializer_class = ImporterSerializer
    pagination_class = GeoNodeApiPagination
    http_method_names = ["get", "post"]

    def get_serializer_class(self):
        specific_serializer = orchestrator.get_serializer(self.request.data)
        return specific_serializer or ImporterSerializer

    def create(self, request, *args, **kwargs):
        """
        Main function called by the new import flow.
        It received the file via the front end
        if is a gpkg (in future it will support all the vector file)
        the new import flow is follow, else the normal upload api is used.
        It clone on the local repo the file that the user want to upload
        """
        _file = request.FILES.get("base_file") or request.data.get("base_file")
        execution_id = None

        serializer = self.get_serializer_class()
        data = serializer(data=request.data)
        storage_manager = None
        # serializer data validation
        data.is_valid(raise_exception=True)
        _data = {
            **data.data.copy(),
            **{
                key: value[0] if isinstance(value, list) else value
                for key, value in request.FILES.items()
            },
        }

        if "zip_file" in _data or "kmz_file" in _data:
            # if a zipfile is provided, we need to unzip it before searching for an handler
            storage_manager = StorageManager(
                remote_files={"base_file": _data.get("zip_file", _data.get("kmz_file"))}
            )
            # cloning and unzip the base_file
            storage_manager.clone_remote_files()
            # update the payload with the unziped paths
            _data.update(storage_manager.get_retrieved_paths())

        handler = orchestrator.get_handler(_data)

        if _file and handler:
            try:
                # cloning data into a local folder
                extracted_params, _data = handler.extract_params_from_data(_data)
                if storage_manager is None:
                    # means that the storage manager is not initialized yet, so
                    # the file is not a zip
                    storage_manager = StorageManager(remote_files=_data)
                    storage_manager.clone_remote_files()
                # get filepath
                files = storage_manager.get_retrieved_paths()

                upload_validator = UploadLimitValidator(request.user)
                upload_validator.validate_parallelism_limit_per_user()
                upload_validator.validate_files_sum_of_sizes(
                    storage_manager.data_retriever
                )

                action = ExecutionRequestAction.IMPORT.value

                execution_id = orchestrator.create_execution_request(
                    user=request.user,
                    func_name=next(iter(handler.get_task_list(action=action))),
                    step=_(next(iter(handler.get_task_list(action=action)))),
                    input_params={
                        **{"files": files, "handler_module_path": str(handler)},
                        **extracted_params,
                    },
                    legacy_upload_name=_file.name,
                    action=action,
                    name=_file.name,
                    source="upload",
                )

                sig = import_orchestrator.s(
                    files, str(execution_id), handler=str(handler), action=action
                )
                sig.apply_async()
                return Response(data={"execution_id": execution_id}, status=201)
            except Exception as e:
                # in case of any exception, is better to delete the
                # cloned files to keep the storage under control
                if storage_manager is not None:
                    storage_manager.delete_retrieved_paths(force=True)
                if execution_id:
                    orchestrator.set_as_failed(execution_id=str(execution_id), reason=e)
                logger.exception(e)
                raise ImportException(detail=e.args[0] if len(e.args) > 0 else e)

        raise ImportException(detail="No handlers found for this dataset type")


class ResourceImporter(DynamicModelViewSet):
    authentication_classes = [
        SessionAuthentication,
        BasicAuthentication,
        OAuth2Authentication,
    ]
    permission_classes = [
        IsAuthenticatedOrReadOnly,
        UserHasPerms(
            perms_dict={
                "dataset": {
                    "PUT": ["base.add_resourcebase", "base.download_resourcebase"],
                    "rule": all,
                },
                "document": {
                    "PUT": ["base.add_resourcebase", "base.download_resourcebase"],
                    "rule": all,
                },
                "default": {"PUT": ["base.add_resourcebase"]},
            }
        ),
    ]
    filter_backends = [
        DynamicFilterBackend,
        DynamicSortingFilter,
        DynamicSearchFilter,
        ExtentFilter,
        ResourceBasePermissionsFilter,
        FavoriteFilter,
    ]
    queryset = ResourceBase.objects.all().order_by("-last_updated")
    serializer_class = ResourceBaseSerializer
    pagination_class = GeoNodeApiPagination

    def copy(self, request, *args, **kwargs):
        resource = self.get_object()
        if resource.resourcehandlerinfo_set.exists():
            handler_module_path = (
                resource.resourcehandlerinfo_set.first().handler_module_path
            )

            action = ExecutionRequestAction.COPY.value

            handler = orchestrator.load_handler(handler_module_path)

            if not handler.can_do(action):
                raise HandlerException(
                    detail=f"The handler {handler_module_path} cannot manage the action required: {action}"
                )

            step = next(iter(handler.get_task_list(action=action)))

            extracted_params, _data = handler.extract_params_from_data(
                request.data, action=action
            )

            execution_id = orchestrator.create_execution_request(
                user=request.user,
                func_name=step,
                step=step,
                action=action,
                input_params={
                    **{"handler_module_path": handler_module_path},
                    **extracted_params,
                },
                source="importer_copy",
            )

            sig = import_orchestrator.s(
                {},
                str(execution_id),
                step=step,
                handler=str(handler_module_path),
                action=action,
                layer_name=resource.title,
                alternate=resource.alternate,
            )
            sig.apply_async()

            # to reduce the work on the FE, the old payload is mantained
            return Response(
                data={
                    "status": "ready",
                    "execution_id": execution_id,
                    "status_url": urljoin(
                        settings.SITEURL,
                        reverse(
                            "rs-execution-status", kwargs={"execution_id": execution_id}
                        ),
                    ),
                },
                status=200,
            )

        return ResourceBaseViewSet(
            request=request, format_kwarg=None, args=args, kwargs=kwargs
        ).resource_service_copy(request, pk=kwargs.get("pk"))
