"""Shared test helpers."""


class AwaitableValue:
    """Awaitable wrapping a fixed value.

    Mimics ``AsyncWeb3``'s property-style accessors (``eth.block_number``,
    ``eth.chain_id``) which return awaitables on each attribute read. Assign
    on a ``MagicMock`` instance — never mutate ``type(mock).attr``, which
    leaks across every other mock in the test process.
    """

    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _resolve():
            return self._value

        return _resolve().__await__()
