from __future__ import (
    absolute_import,
    unicode_literals,
)

from conformity import fields

from pysoa.common.serializer.base import Serializer as BaseSerializer
from pysoa.common.transport.http2_gateway.constants import HTTP2_BACKEND_TYPES

class Http2TransportSchema(fields.Dictionary):
    contents = {
        'backend_layer_kwargs': fields.Dictionary(
            {
                'http_host': fields.UnicodeString(),
                'http_port': fields.UnicodeString(),
            },
            optional_keys=(),
            allow_extra_keys=False,
        ),
        'backend_type': fields.Constant(
            *HTTP2_BACKEND_TYPES,
            description='Which backend (h2 or twisted) should be used for this Http2 transport'
        ),
        'message_expiry_in_seconds': fields.Integer(
            description='How long after a message is sent that it is considered expired, dropped from queue',
        ),
        'queue_capacity': fields.Integer(
            description='The capacity of the message queue to which this transport will send messages',
        ),
        'queue_full_retries': fields.Integer(
            description='How many times to retry sending a message to a full queue before giving up',
        ),
        'receive_timeout_in_seconds': fields.Integer(
            description='How long to block waiting on a message to be received',
        ),
        'default_serializer_config': fields.ClassConfigurationSchema(
            base_class=BaseSerializer,
            description='The configuration for the serializer this transport should use.',
        ),

    }

    optional_keys = (
        'backend_layer_kwargs',
        'message_expiry_in_seconds',
        'queue_capacity',
        'queue_full_retries',
        'receive_timeout_in_seconds',
        'default_serializer_config',
    )

    description = 'The constructor kwargs for the Http2 client and server transports.'
