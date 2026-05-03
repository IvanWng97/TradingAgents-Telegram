"""Telegram bot wrapper around TradingAgents."""

from dotenv import load_dotenv

# Load .env once when the package is first imported. Locally we pick up
# `.env` in the working directory (CWD); inside Docker, env vars are
# already present in os.environ via compose `env_file` and load_dotenv
# is a harmless no-op.
load_dotenv()
