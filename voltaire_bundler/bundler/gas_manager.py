import asyncio
from functools import reduce
import math
from typing import Any
from eth_abi import encode, decode

from eth_utils import keccak

from voltaire_bundler.user_operation.user_operation import UserOperation
from voltaire_bundler.user_operation.user_operation_handler import (
    UserOperationHandler,
)

from voltaire_bundler.bundler.exceptions import (
    ExecutionException,
    ExecutionExceptionCode,
    MethodNotFoundException,
    ValidationException,
    ValidationExceptionCode,
)
from voltaire_bundler.utils.eth_client_utils import (
    send_rpc_request_to_eth_client,
    get_latest_block_info
)
from voltaire_bundler.utils.decode import (
    decode_ExecutionResult,
    decode_FailedOp_event,
    decode_gasEstimateL1Component_result,
)

from voltaire_bundler.utils.encode import (
    encode_handleops_calldata,
    encode_gasEstimateL1Component_calldata,
)

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_VERIFICATION_GAS_LIMIT = 10_000_000
MIN_CALL_GAS_LIMIT = 21_000
MAX_CALL_GAS_LIMIT = 30_000_000

class GasManager:
    ethereum_node_url: str
    chain_id: str
    is_legacy_mode: bool
    max_fee_per_gas_percentage_multiplier: int
    max_priority_fee_per_gas_percentage_multiplier: int
    estimate_gas_with_override_enabled: bool

    def __init__(
        self, 
        ethereum_node_url, 
        chain_id, 
        is_legacy_mode,
        max_fee_per_gas_percentage_multiplier: int,
        max_priority_fee_per_gas_percentage_multiplier: int,
    ):
        self.ethereum_node_url = ethereum_node_url
        self.chain_id = chain_id
        self.is_legacy_mode = is_legacy_mode
        self.max_fee_per_gas_percentage_multiplier = max_fee_per_gas_percentage_multiplier
        self.max_priority_fee_per_gas_percentage_multiplier = max_priority_fee_per_gas_percentage_multiplier
        self.estimate_gas_with_override_enabled = True

    async def estimate_callgaslimit_and_preverificationgas_and_verificationgas(
        self, 
        user_operation: UserOperation,
        entrypoint:str,
        state_override_set_dict:dict[str, Any]
    ) -> [str, str, str]:
        
        latest_block_number, latest_block_basefee, latest_block_gas_limit_hex = await get_latest_block_info(self.ethereum_node_url)
        latest_block_basefee_hex = hex(latest_block_basefee)

        # calculate preverification_gas
        preverification_gas = await self.get_preverification_gas(
            user_operation, entrypoint, latest_block_number, latest_block_basefee
        )
        preverification_gas_hex = hex(preverification_gas)
        user_operation.pre_verification_gas = preverification_gas

        # set verification_gas_limit to MAX_VERIFICATION_GAS_LIMIT to prevent out of gas revert
        user_operation.verification_gas_limit = MAX_VERIFICATION_GAS_LIMIT

        call_gas_limit_hex= await self.estimate_call_gas_limit(
            entrypoint,
            user_operation.sender_address,
            user_operation.init_code,
            user_operation.call_data,
            latest_block_number,
            latest_block_basefee_hex,
            state_override_set_dict,
        )
        verification_gas_hex = await self.estimate_verification_gas_limit(
            user_operation,
            entrypoint,
            latest_block_number,
            latest_block_basefee_hex,
            state_override_set_dict,
        )
        return (
            call_gas_limit_hex,
            preverification_gas_hex,
            verification_gas_hex,
        )
    
    async def estimate_verification_gas_limit(
        self,
        user_operation: UserOperation,
        entrypoint:str,
        block_number_hex: str,
        latest_block_basefee_hex: str,
        state_override_set_dict:dict[str, Any]
    ) -> str:
        user_operation.call_gas_limit = MAX_CALL_GAS_LIMIT
        (
            preOpGas,
            _,
            _,
            _,
        ) = await self.simulate_handle_op(
            user_operation,
            entrypoint,
            block_number_hex,
            latest_block_basefee_hex,
            state_override_set_dict,
        )
        
        verification_gas_limit = preOpGas - user_operation.pre_verification_gas

        verification_gas_hex = hex(verification_gas_limit)

        return verification_gas_hex

    async def estimate_call_gas_limit(
        self,
        entrypoint:str,
        sender_address:str,
        init_code:str,
        call_data:str,
        block_number_hex: str,
        latest_block_basefee_hex: str,
        state_override_set_dict:dict[str, Any],
    ) -> str:
        call_gas_limit_hex = "0x"
        is_state_override_empty_or_none = not bool(state_override_set_dict) or state_override_set_dict is None

        if(
            len(init_code) == 0 and 
            (self.estimate_gas_with_override_enabled or is_state_override_empty_or_none)
        ):
            try:
                call_gas_limit_hex = await self.estimate_call_gas_limit_using_eth_estimate_modified(
                    call_data,
                    entrypoint,
                    sender_address,
                    block_number_hex,
                    state_override_set_dict,
                )
            except MethodNotFoundException:
                self.estimate_gas_with_override_enabled = False
                call_gas_limit_hex = await self.estimate_call_gas_limit_binary_search(
                    entrypoint,
                    sender_address,
                    init_code,
                    call_data,
                    block_number_hex,
                    latest_block_basefee_hex,
                    state_override_set_dict,
                )
        else:
            call_gas_limit_hex = await self.estimate_call_gas_limit_binary_search(
                entrypoint,
                sender_address,
                init_code,
                call_data,
                block_number_hex,
                latest_block_basefee_hex,
                state_override_set_dict,
            )
        return call_gas_limit_hex

    async def find_max_min_gas(
            self,
            entrypoint:str,
            sender_address:str,
            init_code:str,
            call_data:str,
            block_number_hex: str,
            latest_block_basefee_hex: str,
            state_override_set_dict:dict[str, Any],
            gas_used:int
    ):
        success = False
        index = 1
        min_gas = gas_used
        max_gas = 2 * gas_used
        while(max_gas < MAX_CALL_GAS_LIMIT):
            success, gas_used, data = await self.get_call_data_gas_used(
                entrypoint,
                sender_address,
                init_code,
                call_data,
                max_gas,
                block_number_hex,
                latest_block_basefee_hex,
                state_override_set_dict
            )
            if success:
                break
            else:
                index = index + 1
                min_gas = max_gas
                max_gas = math.ceil(2**index * gas_used)

                if max_gas > MAX_CALL_GAS_LIMIT:
                    max_gas = MAX_CALL_GAS_LIMIT

        return max_gas, min_gas

    async def estimate_call_gas_limit_using_eth_estimate_modified(
        self,
        call_data:str,
        entrypoint:str,
        sender_address:str,
        block_number_hex: str,
        state_override_set_dict:dict[str, Any],
    ) -> str:

        call_gas_limit = await self.estimate_call_gas_limit_using_eth_estimate(
            call_data,
            entrypoint,
            sender_address,
            block_number_hex,
            state_override_set_dict,
        )
        #remove call extra calldata cost
        packed_length = len(call_data)
        zero_byte_count = call_data.count(b"\x00")
        non_zero_byte_count = packed_length - zero_byte_count
        call_data_cost = zero_byte_count * 4 + non_zero_byte_count * 16

        call_gas_limit = int(call_gas_limit, 16)- (21000 + call_data_cost)
        call_gas_limit_hex = hex(call_gas_limit)
        return call_gas_limit_hex

    async def estimate_call_gas_limit_binary_search(
        self,
        entrypoint:str,
        sender_address:str,
        init_code:str,
        call_data:str,
        block_number_hex: str,
        latest_block_basefee_hex: str,
        state_override_set_dict:dict[str, Any],
    ) -> str:
        success, gas_used, data = await self.get_call_data_gas_used(
            entrypoint,
            sender_address,
            init_code,
            call_data,
            MAX_CALL_GAS_LIMIT,
            block_number_hex,
            latest_block_basefee_hex,
            state_override_set_dict
        )
        #if not successful with MAX_CALL_GAS_LIMIT, then return EXECUTION_REVERTED
        if not success:
            raise ExecutionException(
                ExecutionExceptionCode.EXECUTION_REVERTED,
                data,
            )
        
        right, left = await self.find_max_min_gas(
            entrypoint,
            sender_address,
            init_code,
            call_data,
            block_number_hex,
            latest_block_basefee_hex,
            state_override_set_dict,
            gas_used
        )

        while(left + 5000 < right):
            mid = left + math.ceil((right-left) / 2)
            success, gas_used, data = await self.get_call_data_gas_used(
                entrypoint,
                sender_address,
                init_code,
                call_data,
                mid,
                block_number_hex,
                latest_block_basefee_hex,
                state_override_set_dict
            )
            if success:
                right = mid
            else:
                left = mid + 1

        call_gas_limit = right
        return hex(call_gas_limit)

    async def get_call_data_gas_used(
        self,
        entrypoint:str,
        sender:str,
        init_code:str,
        call_data:str,
        call_gas_limit:int,
        block_number_hex: str,
        latest_block_basefee: str,
        state_override_set_dict:dict[str, Any]
    ) -> int:

        function_selector = "0x2ab48e82"
        params = encode(
            ["address", "bytes", "bytes", "uint256"], 
            [sender, init_code, call_data, call_gas_limit]
        )
        call_data = function_selector + params.hex()

        default_state_overrides = {
            # GasHelper Bytecode to be deployed at the entrypoint address
            entrypoint: {
                "code": "0x608060405234801561000f575f80fd5b5060043610610034575f3560e01c80632ab48e8214610038578063570e1a3614610063575b5f80fd5b61004b610046366004610261565b61008e565b60405161005a939291906102e6565b60405180910390f35b610076610071366004610343565b61017d565b6040516001600160a01b03909116815260200161005a565b5f80606086156100ff57604051632b870d1b60e11b8152309063570e1a36906100bd908b908b90600401610382565b6020604051808303815f875af11580156100d9573d5f803e3d5ffd5b505050506040513d601f19601f820116820180604052508101906100fd91906103b0565b505b5f5a9050896001600160a01b031685888860405161011e9291906103d2565b5f604051808303815f8787f1925050503d805f8114610158576040519150601f19603f3d011682016040523d82523d5f602084013e61015d565b606091505b5090945091505a61016e90826103e1565b92505096509650969350505050565b5f8061018c6014828587610406565b6101959161042d565b60601c90505f6101a88460148188610406565b8080601f0160208091040260200160405190810160405280939291908181526020018383808284375f92018290525084519495509360209350849250905082850182875af190505f519350806101fc575f93505b50505092915050565b6001600160a01b0381168114610219575f80fd5b50565b5f8083601f84011261022c575f80fd5b50813567ffffffffffffffff811115610243575f80fd5b60208301915083602082850101111561025a575f80fd5b9250929050565b5f805f805f8060808789031215610276575f80fd5b863561028181610205565b9550602087013567ffffffffffffffff8082111561029d575f80fd5b6102a98a838b0161021c565b909750955060408901359150808211156102c1575f80fd5b506102ce89828a0161021c565b979a9699509497949695606090950135949350505050565b83151581525f60208460208401526060604084015283518060608501525f5b8181101561032157858101830151858201608001528201610305565b505f608082860101526080601f19601f83011685010192505050949350505050565b5f8060208385031215610354575f80fd5b823567ffffffffffffffff81111561036a575f80fd5b6103768582860161021c565b90969095509350505050565b60208152816020820152818360408301375f818301604090810191909152601f909201601f19160101919050565b5f602082840312156103c0575f80fd5b81516103cb81610205565b9392505050565b818382375f9101908152919050565b8181038181111561040057634e487b7160e01b5f52601160045260245ffd5b92915050565b5f8085851115610414575f80fd5b83861115610420575f80fd5b5050820193919092039150565b6bffffffffffffffffffffffff19813581811691601485101561045a5780818660140360031b1b83161692505b50509291505056fea2646970667358221220c1f32188b95def9ba16ddcd88c16ae85d53bdec7f0d7ff767d14629aa9489aca64736f6c63430008160033"
            }
        }

        params = [
            {
                "from": ZERO_ADDRESS,
                "to": entrypoint,
                "data": call_data,
                "gasPrice": latest_block_basefee,
            },
            block_number_hex,
            default_state_overrides | state_override_set_dict,
        ]
        result = await send_rpc_request_to_eth_client(
            self.ethereum_node_url, "eth_call", params
        )
        if "error" in result:
            errorMessage = result["error"]["message"]
            errorParams = ""
            if "data" in result["error"]:
                errorData = result["error"]["data"]
                errorParams = errorData[10:]
            raise ExecutionException(
                ExecutionExceptionCode.EXECUTION_REVERTED,
                errorMessage + " " + bytes.fromhex(errorParams[-64:]).decode("ascii"),
            )
        success, gas_used, data = decode(["bool", "uint256", "bytes"], bytes.fromhex(result["result"][2:]))

        return success, gas_used, data

    async def estimate_call_gas_limit_using_eth_estimate(
        self,
        call_data, 
        _from, 
        to,
        block_number_hex = "latest",
        state_override_set_dict = {},
    ):
        if call_data == "0x":
            return "0x"
        
        params = [
            {
                "from": _from,
                "to": to, 
                "data": "0x" + call_data.hex(),
            },
            block_number_hex,
        ]

        is_state_override_empty = not bool(state_override_set_dict)

        if(state_override_set_dict is not None and not is_state_override_empty):
            params.append(state_override_set_dict)

        result = await send_rpc_request_to_eth_client(
            self.ethereum_node_url, "eth_estimateGas", params
        )
        if "error" in result:
            errorMessage = result["error"]["message"]
            errorParams = ""
            if "code" in result["error"]:
                code = result["error"]["code"]
                if code == -32601 or code == -32602:
                    raise MethodNotFoundException(code)

            if "data" in result["error"]:
                errorData = result["error"]["data"]
                errorParams = errorData[10:]
            raise ExecutionException(
                ExecutionExceptionCode.EXECUTION_REVERTED,
                errorMessage + " " + bytes.fromhex(errorParams[-64:]).decode("ascii"),
            )
        
        call_gas_limit = result["result"]
        
        return call_gas_limit

    async def simulate_handle_op(
        self,
        user_operation: UserOperation,
        entrypoint:str,
        block_number_hex: str,
        latest_block_basefee: str,
        state_override_set_dict: dict[str, Any],
        target: str = ZERO_ADDRESS,
        target_call_data: bytes = bytes(0),
    ):
        # simulateHandleOp(entrypoint solidity function) will always revert
        function_selector = "0xd6383f94"
        params = encode(
            [
                "(address,uint256,bytes,bytes,uint256,uint256,uint256,uint256,uint256,bytes,bytes)",  # useroperation
                "address",  # target (Optional - to check the )
                "bytes",  # targetCallData
            ],
            [user_operation.to_list(), target, target_call_data],
        )

        call_data = function_selector + params.hex()

        default_state_overrides = {
            # override the zero address balance with a high value as it is the "from"
            ZERO_ADDRESS: {
                "balance": "0x314dc6448d9338c15b0a00000000"
            },
        }
        #if there is no paymaster, override the sender's balance for gas estimation
        if(len(user_operation.paymaster_and_data) == 0):
            if(target == ZERO_ADDRESS):
                # if the target is zero, simulate_handle_op is called to estimate gas limits
                # override the sender balance with the high value of 10^15 eth
                default_state_overrides[user_operation.sender_address] = {
                    "balance": "0x314dc6448d9338c15b0a00000000"
                }
            else:
                # if the target is not zero, simulate_handle_op is called to detect calldata reverts
                # override the sender deposit slot on the entrypoint contract with the highest deposit value 10^15 eth
                # to detect eth balance reverts. in this cse we don't care about verification gas accuracy
                sender_deposit_slot_index = self.calculate_deposit_slot_index(user_operation.sender_address)
                default_state_overrides[(entrypoint)] = {
                    "stateDiff": {
                        (sender_deposit_slot_index): "0x000000000000000000000000000000000000314dc6448d9338c15b0a00000000" #112 bit allows for 10^15 eth
                    },
                }
        else:
            paymaster_deposit_slot_index = self.calculate_deposit_slot_index("0x" + user_operation.paymaster_and_data[:20].hex())
            default_state_overrides[(entrypoint)] = {
                "stateDiff": {
                    (paymaster_deposit_slot_index): "0x000000000000000000000000000000000000314dc6448d9338c15b0a00000000" #112 bit allows for 10^15 eth
                },
            }

        params = [
            {
                "from": ZERO_ADDRESS,
                "to": entrypoint,
                "data": call_data,
                "gasPrice": latest_block_basefee,
            },
            block_number_hex,
            # default_state_overrides | state_override_set_dict,
        ]
        if(bool(default_state_overrides | state_override_set_dict)):
            params.append(default_state_overrides | state_override_set_dict)

        result = await send_rpc_request_to_eth_client(
            self.ethereum_node_url, "eth_call", params
        )

        if ("error" not in result):
            raise ValueError("simulateHandleOp didn't revert!")

        elif (
            "execution reverted" not in result["error"]["message"] or
            "data" not in result["error"] or len(result["error"]["data"]) < 10
        ):
            raise ValidationException(
                ValidationExceptionCode.SimulateValidation,
                result["error"]["message"],
            )

        error_data = result["error"]["data"]
        solidity_error_selector = str(error_data[:10])
        solidity_error_params = error_data[10:]

        if solidity_error_selector == "0x8b7ac980":
            (
                preOpGas,
                paid,
                targetSuccess,
                targetResult,
            ) = decode_ExecutionResult(solidity_error_params)
        elif solidity_error_selector == "0x220266b6":
            (
                _,
                reason,
            ) = decode_FailedOp_event(solidity_error_params)
            raise ValidationException(
                ValidationExceptionCode.SimulateValidation,
                reason,
            )
        elif solidity_error_selector == "0x08c379a0":  # Error(string)
            reason = decode(
                ["string"], bytes.fromhex(solidity_error_params)
            )  # decode revert message

            raise ValidationException(
                ValidationExceptionCode.SimulateValidation,
                reason[0],
            )
        else:
            raise ValidationException(
                ValidationExceptionCode.SimulateValidation,
                solidity_error_params,
            )

        return preOpGas, paid, targetSuccess, targetResult

    async def verify_gas_fees_and_get_price(
        self, user_operation: UserOperation, enforce_gas_price_tolerance:int
    ) -> int:
        max_fee_per_gas = user_operation.max_fee_per_gas
        max_priority_fee_per_gas = user_operation.max_priority_fee_per_gas

        block_max_fee_per_gas_op = send_rpc_request_to_eth_client(
            self.ethereum_node_url, "eth_gasPrice"
        )

        tasks_arr = [block_max_fee_per_gas_op]

        if not self.is_legacy_mode:
            block_max_priority_fee_per_gas_op = send_rpc_request_to_eth_client(
                self.ethereum_node_url, "eth_maxPriorityFeePerGas"
            )
            tasks_arr.append(block_max_priority_fee_per_gas_op)

        tasks = await asyncio.gather(*tasks_arr)

        block_max_fee_per_gas_hex = tasks[0]["result"]
        block_max_fee_per_gas = int(tasks[0]["result"], 16)
        block_max_fee_per_gas = math.ceil(block_max_fee_per_gas * (self.max_fee_per_gas_percentage_multiplier/100))
        block_max_fee_per_gas_with_tolerance = math.ceil(block_max_fee_per_gas * (1 - (enforce_gas_price_tolerance/100)))
        block_max_fee_per_gas_with_tolerance_hex = hex(block_max_fee_per_gas_with_tolerance)

        if enforce_gas_price_tolerance < 100:
            if self.is_legacy_mode:
                block_max_priority_fee_per_gas = block_max_fee_per_gas
                if max_fee_per_gas < block_max_fee_per_gas_with_tolerance:
                    raise ValidationException(
                        ValidationExceptionCode.SimulateValidation,
                        f"Max fee per gas is too low. it should be minimum : {block_max_fee_per_gas_with_tolerance_hex}",
                    )

            else:
                block_max_priority_fee_per_gas = int(tasks[1]["result"], 16)
                block_max_priority_fee_per_gas = math.ceil(block_max_priority_fee_per_gas * (self.max_priority_fee_per_gas_percentage_multiplier/100))

                estimated_base_fee = max(
                    block_max_fee_per_gas - block_max_priority_fee_per_gas, 1
                )

                if max_fee_per_gas < estimated_base_fee:
                    raise ValidationException(
                        ValidationExceptionCode.InvalidFields,
                        f"Max fee per gas is too low. it should be minimum the estimated base fee: {hex(estimated_base_fee)}",
                    )
                if max_priority_fee_per_gas < 1:
                    raise ValidationException(
                        ValidationExceptionCode.InvalidFields,
                        f"Max priority fee per gas is too low. it should be minimum : 1",
                    )
                if (
                    min(
                        max_fee_per_gas,
                        estimated_base_fee + max_priority_fee_per_gas,
                    )
                    < block_max_fee_per_gas_with_tolerance
                ):
                    raise ValidationException(
                        ValidationExceptionCode.InvalidFields,
                        f"Max fee per gas and (Max priority fee per gas + estimated basefee) should be equal or higher than : {block_max_fee_per_gas_with_tolerance_hex}",
                    )

        return block_max_fee_per_gas_hex

    async def verify_preverification_gas_and_verification_gas_limit(
        self, 
        user_operation: UserOperation,
        entrypoint: str, 
        latest_block_number:str,
        latest_block_basefee:int,
    ) -> None:
        expected_preverification_gas = await self.get_preverification_gas(
            user_operation, entrypoint, latest_block_number, latest_block_basefee
        )

        if user_operation.pre_verification_gas < expected_preverification_gas:
            raise ValidationException(
                ValidationExceptionCode.SimulateValidation,
                f"Preverification gas is too low. it should be minimum : {hex(expected_preverification_gas)}",
            )

        if user_operation.verification_gas_limit > MAX_VERIFICATION_GAS_LIMIT:
            raise ValidationException(
                ValidationExceptionCode.SimulateValidation,
                f"Verification gas is too high. it should be maximum : {hex(MAX_VERIFICATION_GAS_LIMIT)}",
            )

    async def calc_l1_gas_estimate_optimism(
        self, user_operation: UserOperation, 
        block_number_hex: str,
        latest_block_base_fee: int
    ) -> int:

        user_operations_list = [user_operation.to_list()]

        # currently most bundles contains a singler useroperations
        # so l1 fees is calculated for the full handleops transaction 
        handleops_calldata = encode_handleops_calldata(
            user_operations_list, ZERO_ADDRESS
        )

        optimism_gas_oracle_contract_address = (
            "0x420000000000000000000000000000000000000F"
        )

        function_selector = "0x49948e0e" # getL1Fee
        params = encode(
            ["bytes"], 
            [bytes.fromhex(handleops_calldata[2:])]
        )

        call_data = function_selector + params.hex()

        params = [
            {
                "from": ZERO_ADDRESS,
                "to": optimism_gas_oracle_contract_address,
                "data": call_data,
            },
            block_number_hex,
        ]

        result = await send_rpc_request_to_eth_client(
            self.ethereum_node_url, "eth_call", params
        )

        l1_fee = decode(["uint256"], bytes.fromhex(result["result"][2:]))[0]

        l2_gas_price = min(
            user_operation.max_fee_per_gas,
            user_operation.max_priority_fee_per_gas + latest_block_base_fee
        )
        l2_gas_price = max(1, l2_gas_price) #in case l2_gas_price = 0

        gas_estimate_for_l1 = math.ceil(l1_fee / l2_gas_price)

        return gas_estimate_for_l1

    async def calc_l1_gas_estimate_arbitrum(
        self, user_operation: UserOperation, entrypoint:str
    ) -> int:
        arbitrum_nodeInterface_address = (
            "0x00000000000000000000000000000000000000C8"
        )

        is_init: bool = user_operation.nonce == 0

        user_operations_list = [user_operation.to_list()]

        handleops_calldata = encode_handleops_calldata(
            user_operations_list, ZERO_ADDRESS
        )

        call_data = encode_gasEstimateL1Component_calldata(
            entrypoint, is_init, handleops_calldata
        )

        params = [
            {
                "from": ZERO_ADDRESS,
                "to": arbitrum_nodeInterface_address,
                "data": call_data,
            },
            "latest",
        ]

        result = await send_rpc_request_to_eth_client(
            self.ethereum_node_url, "eth_call", params
        )

        raw_gas_results = result["result"]

        gas_estimate_for_l1 = decode_gasEstimateL1Component_result(
            raw_gas_results
        )

        return gas_estimate_for_l1

    async def get_preverification_gas(
        self,
        user_operation: UserOperation,
        entrypoint: str,
        block_number_hex: str,
        latest_block_base_fee: int,
        preverification_gas_percentage_coefficient: int = 100,
        preverification_gas_addition_constant: int = 0,
    ) -> int:
        base_preverification_gas = GasManager.calc_base_preverification_gas(
            user_operation
        )
        l1_gas = 0

        if self.chain_id == 10 or self.chain_id == 420:  # optimism and optimism goerli
            l1_gas = await self.calc_l1_gas_estimate_optimism(
                user_operation, block_number_hex, latest_block_base_fee
            )
        elif self.chain_id == 42161:  # arbitrum One
            l1_gas = await self.calc_l1_gas_estimate_arbitrum(user_operation, entrypoint)

        calculated_preverification_gas = base_preverification_gas + l1_gas

        adjusted_preverification_gas = math.ceil(
            (
                calculated_preverification_gas
                * preverification_gas_percentage_coefficient
                / 100
            )
            + preverification_gas_addition_constant
        )

        return adjusted_preverification_gas

    @staticmethod
    def calc_base_preverification_gas(user_operation: UserOperation) -> int:
        user_operation_list = user_operation.to_list()

        user_operation_list[6] = 21000

        #set a dummy signature only if the user didn't supply any
        if(len(user_operation_list[10]) < 65):
            user_operation_list[
                10
            ] = b"\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01"  # signature

        fixed = 21000
        per_user_operation = 18300
        per_user_operation_word = 4
        zero_byte = 4
        non_zero_byte = 16
        bundle_size = 1
        # sigSize = 65

        packed = UserOperationHandler.pack_user_operation(
            user_operation_list, False
        )
        packed_length = len(packed)
        zero_byte_count = packed.count(b"\x00")
        non_zero_byte_count = packed_length - zero_byte_count
        call_data_cost = zero_byte_count * zero_byte + non_zero_byte_count * non_zero_byte

        length_in_words = math.ceil((packed_length + 31) /32)
        # cost_list = list(
        #     map(lambda x: zero_byte if x == b"\x00" else non_zero_byte, packed)
        # )
        # call_data_cost = reduce(lambda x, y: x + y, cost_list)

        pre_verification_gas = (
            call_data_cost
            + (fixed / bundle_size)
            + per_user_operation
            + per_user_operation_word * length_in_words
        )

        return math.ceil(pre_verification_gas)

    @staticmethod
    def calculate_deposit_slot_index(address, slot = 0): #deposits is at slot 0
        return "0x" + keccak(
                encode(
                    ["uint256", "uint256"],
                    [int(address, 16), slot]
                )
            ).hex()