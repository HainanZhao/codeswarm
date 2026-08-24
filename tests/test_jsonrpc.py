import asyncio
import unittest

from wingmen.jsonrpc import API, APIError


class JSONRPCTests(unittest.TestCase):
    def test_keyword_arguments_are_encoded_by_parameter_name(self) -> None:
        async def scenario() -> None:
            api = API()

            @api.method()
            def configure(name: str, enabled: bool) -> dict: ...

            with api.request() as request:
                configure(name="relay", enabled=True)

            self.assertEqual(
                request.body,
                {
                    "jsonrpc": "2.0",
                    "method": "configure",
                    "params": {"name": "relay", "enabled": True},
                    "id": 1,
                },
            )

        asyncio.run(scenario())

    def test_remote_error_preserves_its_jsonrpc_code(self) -> None:
        async def scenario() -> None:
            api = API()

            @api.method()
            def configure(name: str) -> dict: ...

            with api.request():
                call = configure("relay")
            api.process_response(
                {
                    "jsonrpc": "2.0",
                    "id": call.id,
                    "error": {"code": 409, "message": "conflict", "data": None},
                }
            )

            with self.assertRaisesRegex(APIError, "conflict") as caught:
                await call.wait()
            self.assertEqual(caught.exception.code, 409)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
