import asyncio
import logging

from boto3.dynamodb.table import TableResource

logger = logging.getLogger(__name__)


def register_table_methods(base_classes, **kwargs):
    base_classes.insert(0, CustomTableResource)


class CustomTableResource(TableResource):
    def batch_writer(self, overwrite_by_pkeys=None, flush_amount=25, on_exit_loop_sleep=0):
        return BatchWriter(
            self.name, self.meta.client,
            flush_amount=flush_amount,
            overwrite_by_pkeys=overwrite_by_pkeys,
            on_exit_loop_sleep=on_exit_loop_sleep
        )


class BatchWriter(object):
    """
    Modified so that it does async
    Automatically handle batch writes to DynamoDB for a single table.
    """

    def __init__(
        self, table_name, client, flush_amount=25, overwrite_by_pkeys=None, on_exit_loop_sleep=0
    ):
        """

        :type table_name: str
        :param table_name: The name of the table.  The class handles
            batch writes to a single table.

        :type client: ``botocore.client.Client``
        :param client: A botocore client.  Note this client
            **must** have the dynamodb customizations applied
            to it for transforming AttributeValues into the
            wire protocol.  What this means in practice is that
            you need to use a client that comes from a DynamoDB
            resource if you're going to instantiate this class
            directly, i.e
            ``boto3.resource('dynamodb').Table('foo').meta.client``.

        :type flush_amount: int
        :param flush_amount: The number of items to keep in
            a local buffer before sending a batch_write_item
            request to DynamoDB.

        :type overwrite_by_pkeys: list(string)
        :param overwrite_by_pkeys: De-duplicate request items in buffer
            if match new request item on specified primary keys. i.e
            ``["partition_key1", "sort_key2", "sort_key3"]``

        :type on_exit_loop_sleep: int
        :param on_exit_loop_sleep: When aexit is called by exiting the
            context manager, if the value is > 0 then every time flush
            is called a sleep will also be called.

        """
        self._table_name = table_name
        self._client = client
        self._items_buffer = []
        self._flush_amount = flush_amount
        self._overwrite_by_pkeys = overwrite_by_pkeys
        self._on_exit_loop_sleep = on_exit_loop_sleep

    async def put_item(self, Item):
        await self._add_request_and_process({'PutRequest': {'Item': Item}})

    async def delete_item(self, Key):
        await self._add_request_and_process({'DeleteRequest': {'Key': Key}})

    async def _add_request_and_process(self, request):
        if self._overwrite_by_pkeys:
            self._remove_dup_pkeys_request_if_any(request)
        self._items_buffer.append(request)
        await self._flush_if_needed()

    def _remove_dup_pkeys_request_if_any(self, request):
        pkey_values_new = self._extract_pkey_values(request)
        for item in self._items_buffer:
            if self._extract_pkey_values(item) == pkey_values_new:
                self._items_buffer.remove(item)
                logger.debug("With overwrite_by_pkeys enabled, skipping request:%s", item)

    def _extract_pkey_values(self, request):
        if request.get('PutRequest'):
            return [
                request['PutRequest']['Item'][key]
                for key in self._overwrite_by_pkeys
            ]
        elif request.get('DeleteRequest'):
            return [
                request['DeleteRequest']['Key'][key]
                for key in self._overwrite_by_pkeys
            ]
        return None

    async def _flush_if_needed(self):
        if len(self._items_buffer) >= self._flush_amount:
            await self._flush()

    async def _flush(self):
        items_to_send = self._items_buffer[:self._flush_amount]
        self._items_buffer = self._items_buffer[self._flush_amount:]
        response = await self._client.batch_write_item(
            RequestItems={self._table_name: items_to_send})
        unprocessed_items = response['UnprocessedItems']

        if not unprocessed_items:
            unprocessed_items = {}
        item_list = unprocessed_items.get(self._table_name, [])
        # Any unprocessed_items are immediately added to the
        # next batch we send.
        self._items_buffer.extend(item_list)
        logger.debug(
            "Batch write sent %s, unprocessed: %s, buffer %s",
            len(items_to_send), len(item_list), len(self._items_buffer)
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        # When we exit, we need to keep flushing whatever's left
        # until there's nothing left in our items buffer.
        while self._items_buffer:
            await self._flush()
            if self._items_buffer and self._on_exit_loop_sleep:
                await asyncio.sleep(self._on_exit_loop_sleep)
