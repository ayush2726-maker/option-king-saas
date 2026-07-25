"""Attach Paytm payment-link routes to the existing subscription router."""

from fastapi import APIRouter

from subscription.paytm_routes import router as _paytm_router


if not getattr(APIRouter, "_okai_paytm_link_bootstrap", False):
    _original_apirouter_init = APIRouter.__init__

    def _okai_apirouter_init(self, *args, **kwargs):
        _original_apirouter_init(self, *args, **kwargs)
        if str(kwargs.get("prefix") or "") == "/subscription":
            self.include_router(_paytm_router)
            APIRouter.__init__ = _original_apirouter_init

    APIRouter.__init__ = _okai_apirouter_init
    APIRouter._okai_paytm_link_bootstrap = True
