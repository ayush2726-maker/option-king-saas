from fastapi import HTTPException
import unittest
from unittest.mock import patch

import local_gateway.provisioning_routes as routes


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self):
        self.row = None
        self.committed = False
        self.closed = False

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        if statement.startswith("SELECT * FROM gateway_provision_requests"):
            return _Cursor(self.row)
        if statement.startswith("INSERT INTO gateway_provision_requests"):
            self.row = {
                "user_id": int(params[0]),
                "state": "requested",
                "broker_name": params[1] or None,
                "static_ip": None,
                "instance_id": None,
                "last_error": None,
                "broker_ip_confirmed_at": None,
                "requested_at": params[2],
                "updated_at": params[3],
            }
            return _Cursor()
        raise AssertionError(f"Unexpected SQL: {statement}")

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class GatewayProvisioningOrderTests(unittest.TestCase):
    def test_secure_ip_can_be_requested_before_broker_credentials(self):
        connection = _Connection()
        with (
            patch.object(routes, "get_current_user", return_value={"id": 42, "is_admin": False}),
            patch.object(routes, "entitlement_snapshot", return_value={"live_allowed": True}),
            patch.object(routes, "_selected_broker", return_value=None),
            patch.object(routes, "ensure_schema"),
            patch.object(routes, "get_db", return_value=connection),
            patch.object(routes, "_reconcile_ready", return_value={"paired": False}),
        ):
            result = routes.request_gateway(authorization="Bearer test")

        self.assertTrue(result["success"])
        self.assertIsNone(result["broker"])
        self.assertEqual(result["provisioning"]["state"], "requested")
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)

    def test_secure_ip_still_requires_live_access(self):
        with (
            patch.object(routes, "get_current_user", return_value={"id": 42, "is_admin": False}),
            patch.object(routes, "entitlement_snapshot", return_value={"live_allowed": False}),
        ):
            with self.assertRaises(HTTPException) as context:
                routes.request_gateway(authorization="Bearer test")

        self.assertEqual(context.exception.status_code, 403)

    def test_existing_unsupported_broker_is_rejected(self):
        with (
            patch.object(routes, "get_current_user", return_value={"id": 42, "is_admin": False}),
            patch.object(routes, "entitlement_snapshot", return_value={"live_allowed": True}),
            patch.object(routes, "_selected_broker", return_value={"broker_name": "unsupported"}),
            patch.object(routes, "ensure_schema"),
            patch.object(routes, "_provision_row", return_value=None),
        ):
            with self.assertRaises(HTTPException) as context:
                routes.request_gateway(authorization="Bearer test")

        self.assertEqual(context.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
