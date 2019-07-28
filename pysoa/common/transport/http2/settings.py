from __future__ import (
    absolute_import,
    unicode_literals,
)

from conformity import fields

from pysoa.common.serializer.base import Serializer as BaseSerializer


class Http2TransportSchema(fields.Dictionary):
    contents = {
        'backed_layer_kwargs': fields.Dictionary(
            {
                'http_host': fields.UnicodeString(),
                'http_port': fields.UnicodeString(),
                'log_messages_larger_than_bytes': fields.Integer(
                    description='By default, messages larger than 100KB that do not trigger errors (see '
                                '`maximum_message_size_in_bytes`) will be logged with level WARNING to a logger named '
                                '`pysoa.transport.oversized_message`. To disable this behavior, set this setting to '
                                '0. Or, you can set it to some other number to change the threshold that triggers '
                                'logging.',
                ),
                'maximum_message_size_in_bytes': fields.Integer(
                    description='The maximum message size, in bytes, that is permitted to be transmitted over this '
                                'transport (defaults to 100KB on the client and 250KB on the server)',
                ),
                'message_expiry_in_seconds': fields.Integer(
                    description='How long after a message is sent that it is considered expired, dropped from queue',
                ),
                'receive_timeout_in_seconds': fields.Integer(
                    description='How long to block waiting on a message to be received',
                ),
                'default_serializer_config': fields.ClassConfigurationSchema(
                    base_class=BaseSerializer,
                    description='The configuration for the serializer this transport should use',
                ),
            },
            optional_keys=[
                'log_messages_larger_than_bytes',
                'maximum_message_size_in_bytes',
                'message_expiry_in_seconds',
                'receive_timeout_in_seconds',
                'default_serializer_config',
            ],
            allow_extra_keys=False,
        ),
    }

    optional_keys = ()

    description = 'The settings for the Redis transport'
