# notify.py
#
# XMPP notification sender. All credentials come from the environment
# (a .env file next to the repo root is loaded automatically):
#
#   SH3_NOTIFY_ENABLED   master switch; false/0/no/off disables all
#                        notifications (default: off)
#   SH3_XMPP_JID         sender JID, e.g. "bot@example.com"
#   SH3_XMPP_PASSWORD    password for the sender JID
#   SH3_XMPP_RECIPIENT   recipient JID, e.g. "you@example.com"
import os
import asyncio
import slixmpp

import env_config

env_config.load_env()

NOTIFY_ENABLED = env_config.env_flag('SH3_NOTIFY_ENABLED', default=False)
JID = os.environ.get('SH3_XMPP_JID')
PASSWORD = os.environ.get('SH3_XMPP_PASSWORD')
RECIPIENT = os.environ.get('SH3_XMPP_RECIPIENT')


class XMPPNotifier(slixmpp.ClientXMPP):
    def __init__(self, jid, password):
        slixmpp.ClientXMPP.__init__(self, jid, password)
        self.add_event_handler("session_start", self.start)

    async def start(self, event):
        self.send_presence()
        await self.get_roster()


def _missing_config():
    missing = [name for name, value in (
        ('SH3_XMPP_JID', JID),
        ('SH3_XMPP_PASSWORD', PASSWORD),
        ('SH3_XMPP_RECIPIENT', RECIPIENT),
    ) if not value]
    if missing:
        print("[-] XMPP notifications enabled but not configured. "
              f"Set {', '.join(missing)} in the environment or .env.")
        return True
    return False


async def send_xmpp_message(message_body):
    if _missing_config():
        return

    xmpp = XMPPNotifier(JID, PASSWORD)

    # Connect is blocking (returns True/False), but we run it inside the async loop
    connected = xmpp.connect()

    if connected:
        # We don't use process() anymore.
        # We just wait for the connection to be fully established via 'disconnected' event or timeout
        # A simple sleep is usually enough for the handshake to complete

        try:
            # Wait for session_start event to fire
            # We can check a flag or just wait 2 seconds as a heuristic
            await asyncio.sleep(2)

            xmpp.send_message(
                mto=RECIPIENT,
                mbody=message_body,
                mtype='chat'
            )
            print(f"[+] Notification sent.")
        except Exception as e:
            print(f"[-] XMPP Send Error: {e}")
        finally:
            # Stop the event loop and disconnect
            xmpp.disconnect()
    else:
        print("[-] Could not connect to XMPP server.")


# Helper to run the async function from a sync script
def trigger_notification(text):
    if not NOTIFY_ENABLED:
        return
    try:
        asyncio.run(send_xmpp_message(text))
    except Exception as e:
        print(f"[-] Async Loop Error: {e}")