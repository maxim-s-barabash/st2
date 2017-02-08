# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import six

from oslo_config import cfg
from mongoengine import ValidationError
from webob import Response

from st2api.controllers.base import BaseRestControllerMixin
from st2common import log as logging
from st2common.models.api.auth import ApiKeyAPI, ApiKeyCreateResponseAPI
from st2common.constants.secrets import MASKED_ATTRIBUTE_VALUE
from st2common.exceptions.auth import ApiKeyNotFoundError
from st2common.persistence.auth import ApiKey
from st2common.router import abort
from st2common.util import auth as auth_util
from st2common.util.jsonify import json_encode

http_client = six.moves.http_client

LOG = logging.getLogger(__name__)

__all__ = [
    'ApiKeyController'
]


class ApiKeyController(BaseRestControllerMixin):
    """
    Implements the REST endpoint for managing the key value store.
    """

    supported_filters = {
        'user': 'user'
    }

    query_options = {
        'sort': ['user']
    }

    def __init__(self):
        super(ApiKeyController, self).__init__()
        self.get_one_db_method = ApiKey.get_by_key_or_id

    # @request_user_has_resource_db_permission(permission_type=PermissionType.API_KEY_VIEW)
    def get_one(self, api_key_id_or_key, requester_user, show_secrets=None):
        """
            List api keys.

            Handle:
                GET /apikeys/1
        """
        api_key_db = None
        try:
            api_key_db = ApiKey.get_by_key_or_id(api_key_id_or_key)
        except ApiKeyNotFoundError:
            msg = 'ApiKey matching %s for reference and id not found.', api_key_id_or_key
            LOG.exception(msg)
            abort(http_client.NOT_FOUND, msg)

        try:
            mask_secrets = self._get_mask_secrets(show_secrets=show_secrets,
                                                  requester_user=requester_user)
            return ApiKeyAPI.from_model(api_key_db, mask_secrets=mask_secrets)
        except (ValidationError, ValueError) as e:
            LOG.exception('Failed to serialize API key.')
            abort(http_client.INTERNAL_SERVER_ERROR, str(e))

    # @request_user_has_permission(permission_type=PermissionType.API_KEY_LIST)
    def get_all(self, requester_user, show_secrets=None, **kw):
        """
            List all keys.

            Handles requests:
                GET /apikeys/
        """
        mask_secrets = self._get_mask_secrets(show_secrets=show_secrets,
                                              requester_user=requester_user)
        api_key_dbs = ApiKey.get_all(**kw)
        api_keys = [ApiKeyAPI.from_model(api_key_db, mask_secrets=mask_secrets)
                    for api_key_db in api_key_dbs]

        return api_keys

    # @request_user_has_resource_api_permission(permission_type=PermissionType.API_KEY_CREATE)
    def post(self, api_key_api, requester_user):
        """
        Create a new entry.
        """
        api_key_db = None
        api_key = None
        try:
            if not getattr(api_key_api, 'user', None):
                api_key_api.user = requester_user or cfg.CONF.system_user.user
            # If key_hash is provided use that and do not create a new key. The assumption
            # is user already has the original api-key
            if not getattr(api_key_api, 'key_hash', None):
                api_key, api_key_hash = auth_util.generate_api_key_and_hash()
                # store key_hash in DB
                api_key_api.key_hash = api_key_hash
            api_key_db = ApiKey.add_or_update(ApiKeyAPI.to_model(api_key_api))
        except (ValidationError, ValueError) as e:
            LOG.exception('Validation failed for api_key data=%s.', api_key_api)
            abort(http_client.BAD_REQUEST, str(e))

        extra = {'api_key_db': api_key_db}
        LOG.audit('ApiKey created. ApiKey.id=%s' % (api_key_db.id), extra=extra)

        api_key_create_response_api = ApiKeyCreateResponseAPI.from_model(api_key_db)
        # Return real api_key back to user. A one-way hash of the api_key is stored in the DB
        # only the real value only returned at create time. Also, no masking of key here since
        # the user needs to see this value atleast once.
        api_key_create_response_api.key = api_key

        resp = Response(body=json_encode(api_key_create_response_api), status=http_client.CREATED)
        resp.headers['Content-Type'] = 'application/json'
        return resp

    # @request_user_has_resource_db_permission(permission_type=PermissionType.API_KEY_MODIFY)
    def put(self, api_key_api, api_key_id_or_key):
        api_key_db = ApiKey.get_by_key_or_id(api_key_id_or_key)

        LOG.debug('PUT /apikeys/ lookup with api_key_id_or_key=%s found object: %s',
                  api_key_id_or_key, api_key_db)

        old_api_key_db = api_key_db
        api_key_db = ApiKeyAPI.to_model(api_key_api)

        # Passing in key_hash as MASKED_ATTRIBUTE_VALUE is expected since we do not
        # leak it out therefore it is expected we get the same value back. Interpret
        # this special code and empty value as no-change
        if api_key_db.key_hash == MASKED_ATTRIBUTE_VALUE or not api_key_db.key_hash:
            api_key_db.key_hash = old_api_key_db.key_hash

        # Rather than silently ignore any update to key_hash it is better to explicitly
        # disallow and notify user.
        if old_api_key_db.key_hash != api_key_db.key_hash:
            raise ValueError('Update of key_hash is not allowed.')

        api_key_db.id = old_api_key_db.id
        api_key_db = ApiKey.add_or_update(api_key_db)

        extra = {'old_api_key_db': old_api_key_db, 'new_api_key_db': api_key_db}
        LOG.audit('API Key updated. ApiKey.id=%s.' % (api_key_db.id), extra=extra)
        api_key_api = ApiKeyAPI.from_model(api_key_db)

        return api_key_api

    # @request_user_has_resource_db_permission(permission_type=PermissionType.API_KEY_DELETE)
    def delete(self, api_key_id_or_key):
        """
            Delete the key value pair.

            Handles requests:
                DELETE /apikeys/1
        """
        api_key_db = ApiKey.get_by_key_or_id(api_key_id_or_key)

        LOG.debug('DELETE /apikeys/ lookup with api_key_id_or_key=%s found object: %s',
                  api_key_id_or_key, api_key_db)

        ApiKey.delete(api_key_db)

        extra = {'api_key_db': api_key_db}
        LOG.audit('ApiKey deleted. ApiKey.id=%s' % (api_key_db.id), extra=extra)

        return Response(status=http_client.NO_CONTENT)

api_key_controller = ApiKeyController()
