import bittensor as bt
from bittensor.utils.balance import Balance
from bittensor.core.extrinsics.mev_shield import submit_encrypted_extrinsic


NETWORK = 'test'

OWNER_WALLET_NAME = 'greevils-owner'
HOTKEY = '5DHeJ88aJ6amyxRbXbteGTSjJgGwztYptfTb4ap65JHZrt2e'

NETUID = 506
BUY_AMOUNT = 10


def main():
    owner_wallet = bt.Wallet(OWNER_WALLET_NAME)
    
    subtensor = bt.Subtensor(network=NETWORK)
    substrate = subtensor.substrate
    
    start_call = substrate.compose_call(
        call_module='SubtensorModule',
        call_function='start_call',
        call_params={
            "netuid": NETUID,
        },
    )

    buy_call = substrate.compose_call(
        call_module='SubtensorModule',
        call_function='add_stake',
        call_params={
            "netuid": NETUID,
            "hotkey": HOTKEY,
            "amount_staked": Balance.from_tao(BUY_AMOUNT).rao
        },
    )

    calls = [start_call, buy_call]

    batch_call = substrate.compose_call(
        call_module='Utility',
        call_function='batch_all',
        call_params={
            "calls": calls
        }
    )

    receipt = submit_encrypted_extrinsic(
        subtensor=subtensor,
        wallet=owner_wallet,
        call=batch_call,
        period=None,
        raise_error=False,
        wait_for_inclusion=True,
        wait_for_finalization=False,
        wait_for_revealed_execution=False,
    )

    is_success = receipt.success
    message = receipt.message

    print(f"is_success: {is_success}")
    print(f"message: {message}")



if __name__ == '__main__':
    main()
