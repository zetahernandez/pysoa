from __future__ import (
    absolute_import,
    unicode_literals,
)

import attr
import six


class UnicodeKeysDict(dict):
    def __setitem__(self, key, value):
        super(UnicodeKeysDict, self).__setitem__(six.text_type(key), value)

    def setdefault(self, key, default=None):
        super(UnicodeKeysDict, self).setdefault(six.text_type(key), default)


@attr.s(frozen=True)
class Error(object):
    """The error generated by a single action."""
    code = attr.ib()
    message = attr.ib()
    field = attr.ib(default=None)
    traceback = attr.ib(default=None)
    variables = attr.ib(default=None)
    denied_permissions = attr.ib(default=None)


@attr.s
class ActionRequest(object):
    """A request that the server execute a single action."""
    action = attr.ib()
    body = attr.ib(default=attr.Factory(dict))


@attr.s
class ActionResponse(object):
    """A response generated by a single action on the server."""
    action = attr.ib()
    errors = attr.ib(
        default=attr.Factory(list),
        converter=lambda l: [e if isinstance(e, Error) else Error(**e) for e in l],
    )
    body = attr.ib(default=attr.Factory(dict))


@attr.s
class JobRequest(object):
    """
    A request that the server execute a job.

    A job consists of one or more actions and a control header. Each action is an ActionRequest,
    while the control header is a dictionary.
    """
    control = attr.ib(default=attr.Factory(dict))
    context = attr.ib(default=attr.Factory(dict))
    actions = attr.ib(
        default=attr.Factory(list),
        converter=lambda l: [a if isinstance(a, ActionRequest) else ActionRequest(**a) for a in l],
    )


@attr.s
class JobResponse(object):
    """
    A response generated by a server job.

    Contains the result or error generated by each action in the job.
    """
    errors = attr.ib(
        default=attr.Factory(list),
        converter=lambda l: [e if isinstance(e, Error) else Error(**e) for e in l],
    )
    context = attr.ib(default=attr.Factory(dict))
    actions = attr.ib(
        default=attr.Factory(list),
        converter=lambda l: [a if isinstance(a, ActionResponse) else ActionResponse(**a) for a in l],
    )
