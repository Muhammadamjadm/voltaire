from dataclasses import dataclass
import asyncio
from voltaire_bundler.user_operation.user_operation import UserOperation
from .sender import Sender
from voltaire_bundler.user_operation.user_operation_handler import UserOperationHandler
from .validation_manager import ValidationManager
from .reputation_manager import ReputationManager, ReputationStatus
from voltaire_bundler.bundler.exceptions import ValidationException, ValidationExceptionCode


@dataclass
class MempoolManager:
    validation_manager: ValidationManager
    user_operation_handler: UserOperationHandler
    reputation_manager: ReputationManager
    ethereum_node_url: str
    bundler_private_key: str
    bundler_address: str
    entrypoint: str
    chain_id: int
    senders: dict[str, Sender]
    is_unsafe: bool
    entity_no_of_ops_in_mempool: dict[str, int]  # factory and paymaster

    def __init__(
        self,
        validation_manager: ValidationManager,
        user_operation_handler: UserOperationHandler,
        reputation_manager: ReputationManager,
        ethereum_node_url: str,
        bundler_private_key: str,
        bundler_address: str,
        entrypoint: str,
        chain_id: int,
        is_unsafe: bool,
    ):
        self.validation_manager = validation_manager
        self.user_operation_handler = user_operation_handler
        self.reputation_manager = reputation_manager
        self.ethereum_node_url = ethereum_node_url
        self.bundler_private_key = bundler_private_key
        self.bundler_address = bundler_address
        self.entrypoint = entrypoint
        self.chain_id = chain_id
        self.is_unsafe = is_unsafe
        self.senders = {}
        self.entity_no_of_ops_in_mempool = {}

    def clear_user_operations(self) -> None:
        self.senders.clear()

    async def add_user_operation(self, user_operation: UserOperation) -> str:
        self._verify_entities_reputation(
            user_operation.sender,
            user_operation.factory_address,
            user_operation.paymaster_address,
        )

        is_sender_staked = await self.validation_manager.validate_user_operation(
            user_operation,
        )

        new_sender = None
        new_sender_address = user_operation.sender

        if new_sender_address not in self.senders:
            self.senders[new_sender_address] = Sender(new_sender_address)

        new_sender = self.senders[new_sender_address]

        tasks = await asyncio.gather(
            new_sender.add_user_operation(
                user_operation,
                is_sender_staked,
            ),
            asyncio.to_thread(
                UserOperationHandler.get_user_operation_hash,
                    user_operation.to_list(),
                    self.entrypoint,
                    self.chain_id
            ),
        )

        user_operation_hash = tasks[1]

        self.update_all_seen_status(
            user_operation.sender,
            user_operation.factory_address,
            user_operation.paymaster_address,
        )

        if user_operation.factory_address is not None:
            self._update_entity_no_of_ops_in_mempool(
                user_operation.factory_address
            )

        if user_operation.paymaster_address is not None:
            self._update_entity_no_of_ops_in_mempool(
                user_operation.paymaster_address
            )
        
        return user_operation_hash

    async def get_user_operations_to_bundle(self) -> list[UserOperation]:
        bundle = []
        validation_operations = []
        for sender_address in list(self.senders):
            sender = self.senders[sender_address]
            if len(sender.user_operations) > 0:
                user_operation = sender.user_operations.pop(0)

                new_code_hash = (
                    await self.validation_manager.get_addresses_code_hash(
                        user_operation.associated_addresses
                    )
                )
                if new_code_hash != user_operation.code_hash:
                    continue

                bundle.append(user_operation)
                if len(sender.user_operations) == 0:
                    del self.senders[sender.address]

        await asyncio.gather(*validation_operations)

        return bundle

    def get_all_user_operations(self) -> list[UserOperation]:
        user_operations = [
            user_operation
            for sender in self.senders.values()
            for user_operation in sender.user_operations
        ]
        return user_operations

    def update_all_seen_status(
        self, sender_address: str, factory_address: str, paymaster_address: str
    ) -> None:
        self.reputation_manager.update_seen_status(sender_address)

        if factory_address is not None:
            self.reputation_manager.update_seen_status(factory_address)

        if paymaster_address is not None:
            self.reputation_manager.update_seen_status(paymaster_address)

    def _verify_entities_reputation(
        self, sender_address: str, factory_address: str, paymaster_address: str
    ) -> None:
        sender_no_of_ops = 0
        if sender_address in self.senders:
            sender_no_of_ops = len(
                self.senders[sender_address].user_operations
            )
        self._verify_entity_reputation(
            sender_address, "sender", sender_no_of_ops
        )

        if factory_address is not None:
            factory_no_of_ops = 0
            if factory_address in self.entity_no_of_ops_in_mempool:
                factory_no_of_ops = self.entity_no_of_ops_in_mempool[
                    factory_address
                ]
            self._verify_entity_reputation(
                factory_address,
                "factory",
                factory_no_of_ops,
            )

        if paymaster_address is not None:
            paymaster_no_of_ops = 0
            if paymaster_address in self.entity_no_of_ops_in_mempool:
                paymaster_no_of_ops = self.entity_no_of_ops_in_mempool[
                    paymaster_address
                ]
            self._verify_entity_reputation(
                paymaster_address,
                "paymaster",
                paymaster_no_of_ops,
            )

    def _verify_entity_reputation(
        self, entity_address: str, entity_name: str, entity_no_of_ops: int
    ) -> None:
        if entity_address not in self.entity_no_of_ops_in_mempool:
            self.entity_no_of_ops_in_mempool[entity_address] = 0

        entity_no_of_ops = self.entity_no_of_ops_in_mempool[entity_address]
        status = self.reputation_manager.get_status(entity_address)
        if status == ReputationStatus.BANNED:
            raise ValidationException(
                ValidationExceptionCode.Reputation,
                " ".join(
                    (
                        "user operation was dropped because ",
                        entity_address,
                        "is banned",
                        entity_name,
                    )
                ),
                "",
            )
        elif status == ReputationStatus.THROTTLED and entity_no_of_ops > 0:
            raise ValidationException(
                ValidationExceptionCode.Reputation,
                " ".join(
                    (
                        "user operation was dropped",
                        entity_address,
                        "is throttled",
                        entity_name,
                    )
                ),
                "",
            )

    def _update_entity_no_of_ops_in_mempool(self, entity_address: str) -> None:
        no_of_ops = 0
        if entity_address in self.entity_no_of_ops_in_mempool:
            no_of_ops = self.entity_no_of_ops_in_mempool[entity_address]
        self.entity_no_of_ops_in_mempool[entity_address] = no_of_ops + 1
