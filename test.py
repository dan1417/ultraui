import bittensor as bt
from bittensor.core.extrinsics.mev_shield import submit_encrypted_extrinsic

subtensor = bt.Subtensor(network="finney")

wallet = bt.Wallet(name="choji")

call = subtensor.substrate.compose_call(
    call_module='SubtensorModule',
    call_function='register_network',
    call_params={
        'hotkey': '5DcjNsy576sztnSHEsLTRc8PoyZfXNupQDXiVgGWbHyJcqbC',
    }
)

receipt = submit_encrypted_extrinsic(
    subtensor=subtensor,
    wallet=wallet,
    call=call,
    period=None,
    raise_error=False,
    wait_for_inclusion=True,
    wait_for_finalization=False,
    wait_for_revealed_execution=False,
)

is_success = receipt.success
message = receipt.message