import logging
import time
from uuid import uuid4

from ..jsonutils.pointer import fragment_decode
from .callback import CallbackServer

LOG = logging.getLogger(__name__)


class ResourceClient:

    CREATE = "CREATE"
    READ = "READ"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    LIST = "LIST"
    ALREADY_EXISTS = "AlreadyExists"
    NOT_UPDATABLE = "NotUpdatable"
    NOT_FOUND = "NotFound"
    NO_OP = "NoOperationToPerform"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    ACK_TIMEOUT = 3

    def __init__(self, transport, resource_def):
        self._transport = transport
        self._resource_def = resource_def

    def get_identifier_property(self, resource, writable=False):
        encoded_writable_identifiers = set(self._resource_def["identifiers"]) - set(
            self._resource_def.get("readOnly", ())
        )
        writable_identifiers = {
            fragment_decode(identifier)[-1]
            for identifier in encoded_writable_identifiers
        }
        writable_identifiers = resource["properties"].keys() & writable_identifiers
        # If a writable identifier exists, this returns that, because
        # writable identifiers are required in resource cleanup on tests
        # where resources get deleted/updated in the test body
        if writable_identifiers:
            identifier = writable_identifiers.pop()
        elif not writable:
            id_reference = fragment_decode(self._resource_def["identifiers"][0])
            identifier = id_reference[-1]
        else:
            identifier = None
        return identifier

    def wait_for_specified_event(
        self, listener, specified_event, timeout_in_seconds=60
    ):
        events = []
        start_time = time.time()
        specified = False
        while ((time.time() - start_time) < timeout_in_seconds) and not specified:
            time.sleep(0.5)
            while listener.events:
                event = listener.events.popleft()
                events.append(event)
                if event.get("OperationStatus", "") in (specified_event, self.FAILED):
                    specified = True
        return events

    def prepare_request(
        self, operation, token=None, resource=None, previous_resource=None
    ):
        if not token:
            token = str(uuid4())
        request = {
            "requestContext": {
                "resourceType": self._resource_def["typeName"],
                "operation": operation,
                "clientRequestToken": token,
            },
            "requestData": {},
        }
        if resource:
            request["requestData"]["resourceProperties"] = resource["properties"]
        if previous_resource:
            request["requestData"]["previousResourceProperties"] = previous_resource[
                "properties"
            ]
        return request, token

    @staticmethod
    def verify_events_contain_token(events, token):
        if any(event["BearerToken"] != token for event in events):
            raise AssertionError(
                "Request tokens:\n"
                + "\n".join(event["BearerToken"] for event in events)
            )

    def create_resource(self, resource):
        request, token = self.prepare_request(
            self.CREATE, resource["type"], resource=resource
        )
        events = self.send_async_request(request, token, self.COMPLETE)
        return events[-1]

    def read_resource(self, resource):
        id_key = self.get_identifier_property(resource)
        id_resource = {"type": resource["type"]}
        id_resource["properties"] = {id_key: resource["properties"][id_key]}
        request, token = self.prepare_request(self.READ, resource=id_resource)
        return self.send_sync_request(request, token)

    def update_resource(self, resource, updated_resource):
        request, token = self.prepare_request(
            self.UPDATE, resource=updated_resource, previous_resource=resource
        )
        events = self.send_async_request(request, token, self.COMPLETE)
        return events[-1]

    def delete_resource(self, resource):
        id_key = self.get_identifier_property(resource)
        id_resource = {"type": resource["type"]}
        id_resource["properties"] = {id_key: resource["properties"][id_key]}
        request, token = self.prepare_request(self.DELETE, resource=id_resource)
        events = self.send_async_request(request, token, self.COMPLETE)
        return events[-1]

    def list_resources(self):
        request, token = self.prepare_request(self.LIST)
        return self.send_sync_request(request, token)

    def send_request_for_ack(self, operation):
        request, token = self.prepare_request(operation)
        events = self.send_async_request(request, token, self.IN_PROGRESS)
        return events[0]

    def compare_requested_model(self, requested_model, returned_model):
        # Do not need to check write only properties in requested model.
        write_only_properties = {
            fragment_decode(prop)[-1]
            for prop in self._resource_def.get("writeOnly", ())
        }
        comparable_properties = (
            requested_model["properties"].keys() - write_only_properties
        )
        for key in comparable_properties:
            assert (
                returned_model["properties"][key] == requested_model["properties"][key]
            )

    def send_async_request(self, request, token, status):
        with CallbackServer() as listener:
            self._transport(request, callback_endpoint=listener.server_address)
            events = self.wait_for_specified_event(listener, status)
        self.verify_events_contain_token(events, token)
        assert events
        return events

    def send_sync_request(self, request, token):
        return_event = self._transport(request, None)
        self.verify_events_contain_token([return_event], token)
        return return_event