import logging
import json
import sys

from pycoin.tx.script.tools import disassemble

from gaservices.utils import gacommon, gaconstants, txutil
import wallycore as wally

from . import clargs
from . import exceptions
from . import ga_xpub


class TwoOfTwo:

    def __init__(self, mnemonic, seed, nlocktime_file):
        logging.info('Reading nlocktime transactions from {}'.format(nlocktime_file))
        self.compressed_zip = open(nlocktime_file, "rb").read()

        self.mnemonic = mnemonic
        self.seed = seed
        version = wally.BIP32_VER_MAIN_PRIVATE
        self.wallet = wally.bip32_key_from_seed(self.seed, version, wally.BIP32_FLAG_SKIP_HASH)
        chaincode = wally.bip32_key_get_chain_code(self.wallet)

        zipdata = gacommon._unzip(self.compressed_zip, chaincode)
        if len(zipdata) == 0:
            raise exceptions.GARecoveryError(
                'The nlocktimes file "{}" contains no transactions'.format(nlocktime_file))
        self.txdata = [json.loads(txdata.decode("ascii")) for txdata in zipdata]

        self.fixup_old_nlocktimes()

        self.is_testnet = self._is_testnet()

    def fixup_old_nlocktimes(self):
        """Fixup data from old format nlocktimes files

        Older nlocktimes files do not contain explicit prevout_signatures, prevout_scripts or
        prevout_script_types. Detect this and extract them from the raw transaction to make the
        txdata look consistent to the rest of the code. Note that segwit is not being handled
        here because old style nlocktimes predate segwit
        """
        for txdata in self.txdata:
            if 'prevout_signatures' not in txdata:
                tx = txutil.from_hex(txdata['tx'])
                txdata['prevout_script_types'] = []
                txdata['prevout_signatures'] = []
                txdata['prevout_scripts'] = []
                for i in range(wally.tx_get_num_inputs(tx)):
                    dis = disassemble(wally.tx_get_input_script(tx, i)).split()
                    _, ga_signature, _, redeem_script = dis
                    txdata['prevout_signatures'].append(ga_signature[1:-1])
                    txdata['prevout_scripts'].append(redeem_script[1:-1])
                    txdata['prevout_script_types'].append(gaconstants.P2SH_FORTIFIED_OUT)

    def _is_testnet(self):
        """Return true if the GreenAddress xpub for testnet is found in the redeem script

        This is determined by generating the sets of possible GreenAddress public keys for each
        network (testnet/mainnet) and then searching for them in the redeem script
        """
        pointer = self.txdata[0]['prevout_pointers'][0]
        subaccount = self.txdata[0]['prevout_subaccounts'][0]

        def get_pubkey_for_pointer_hex(xpub):
            """Return hex encoded public key derived from xpub for pointer"""
            xpub = gacommon.derive_hd_key(xpub, [pointer], wally.BIP32_FLAG_KEY_PUBLIC)
            return wally.hex_from_bytes(wally.bip32_key_get_pub_key(xpub))

        def get_pubkeys_hex(fn, key_material, testnet):
            """Return a list of hex-encoded public key given either a seed or a mnemonic"""
            xpubs = fn(key_material, subaccount, testnet)
            return [get_pubkey_for_pointer_hex(xpub) for xpub in xpubs]

        def get_pubkeys_for_network_hex(testnet):
            """Return all the possible ga public keys (hex encoded) for testnet/non-testnet"""
            pubkeys_hex = get_pubkeys_hex(ga_xpub.xpubs_from_seed, self.seed, testnet)
            if self.mnemonic:
                pubkeys_hex.extend(
                    get_pubkeys_hex(ga_xpub.xpubs_from_mnemonic, self.mnemonic, testnet))
            return pubkeys_hex

        mainnet_xpubs = get_pubkeys_for_network_hex(testnet=False)
        testnet_xpubs = get_pubkeys_for_network_hex(testnet=True)

        redeem_script = self.txdata[0]['prevout_scripts'][0]
        if any(xpub in redeem_script for xpub in mainnet_xpubs):
            return False
        if any(xpub in redeem_script for xpub in testnet_xpubs):
            return True

        # Default to mainnet
        # Generally one of the derived xpubs will be found in the redeem script. It's possible
        # if the xpub was derived from the variant of the gait path using the mnemonic but a
        # hex seed was provided instead of a mnemonic when running the recovery tool that the key
        # will not be found. In this case default to mainnet.
        logging.warn("Unable to detect network. Defaulting to mainnet. Consider "
                     "passing the full mnemonic rather than hex seed")
        return False

    def _get_signed_tx(self, txdata):
        key = gacommon.derive_user_private_key(txdata, self.wallet, branch=1)
        return gacommon.countersign(txdata, key)

    def _get_private_key_wif(self, txdata):
        key = gacommon.derive_user_private_key(txdata, self.wallet, branch=4)
        return gacommon.private_key_to_wif(key, self.is_testnet)

    def get_transactions(self):
        txs = []
        for txdata in self.txdata:
            tx = self._get_signed_tx(txdata)
            private_key_wif = self._get_private_key_wif(txdata)
            txs.append((tx, private_key_wif))
        return txs
