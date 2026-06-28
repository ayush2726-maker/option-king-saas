from abc import ABC, abstractmethod
from typing import Optional

class BaseBroker(ABC):
    def __init__(self, client_id, api_key, api_secret, totp_secret=None):
        self.client_id = client_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.totp_secret = totp_secret
        self.is_logged_in = False
        self.access_token = None

    @abstractmethod
    def login(self) -> dict: pass
    @abstractmethod
    def logout(self) -> dict: pass
    @abstractmethod
    def get_ltp(self, symbol, exchange="NFO") -> dict: pass
    @abstractmethod
    def get_candles(self, symbol, interval, from_date, to_date, exchange="NFO") -> dict: pass
    @abstractmethod
    def search_option(self, underlying, expiry, strike, option_type) -> dict: pass
    @abstractmethod
    def place_order(self, symbol, token, transaction_type, quantity, order_type="MARKET", price=0, exchange="NFO") -> dict: pass
    @abstractmethod
    def get_order_status(self, order_id) -> dict: pass
    @abstractmethod
    def get_positions(self) -> dict: pass
    @abstractmethod
    def close_position(self, symbol, token, quantity, exchange="NFO") -> dict: pass
    @abstractmethod
    def get_funds(self) -> dict: pass
    @classmethod
    @abstractmethod
    def broker_name(cls) -> str: pass
    @classmethod
    @abstractmethod
    def display_name(cls) -> str: pass
    @classmethod
    @abstractmethod
    def required_fields(cls) -> list: pass
    @classmethod
    @abstractmethod
    def setup_guide(cls) -> dict: pass
