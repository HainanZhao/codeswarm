import asyncio
import unittest

from wingmen.jsonrpc import API, APIError, ErrorCode, Server


class JSONRPCTests(unittest.TestCase):
    def test_server_rejects_missing_and_unexpected_parameters(self) -> None:
        async def scenario() -> None:
            server = Server()

            @server.method()
            def add(a: int, b: int) -> int:
                return a + b

            requests = (
                {"params": [1]},
                {"params": [1, 2, 3]},
                {"params": {"a": 1, "b": 2, "ignored": 3}},
            )
            for request in requests:
                with self.subTest(params=request["params"]):
                    response = await server.call(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "add",
                            **request,
                        }
                    )
                    self.assertIsInstance(response, dict)
                    assert isinstance(response, dict)
                    error = response.get("error")
                    self.assertIsInstance(error, dict)
                    assert isinstance(error, dict)
                    self.assertEqual(error["code"], ErrorCode.INVALID_PARAMS)

        asyncio.run(scenario())

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

    def test_response_owner_must_match_the_request_owner(self) -> None:
        async def scenario() -> None:
            api = API()
            owner = object()
            other_owner = object()

            @api.method()
            def configure(name: str) -> dict: ...

            with api.request(owner=owner):
                call = configure("relay")

            response = {"jsonrpc": "2.0", "id": call.id, "result": {"ok": True}}
            api.process_response(response, owner=other_owner)
            self.assertFalse(call.future.done())

            api.process_response(response, owner=owner)
            self.assertEqual(await call.wait(), {"ok": True})

        asyncio.run(scenario())

    def test_malformed_matching_response_fails_the_call(self) -> None:
        async def scenario() -> None:
            api = API()

            @api.method()
            def configure(name: str) -> dict: ...

            for malformed_payload in ({}, {"error": "not an error object"}):
                with self.subTest(payload=malformed_payload):
                    with api.request():
                        call = configure("relay")
                    api.process_response(
                        {"jsonrpc": "2.0", "id": call.id, **malformed_payload}
                    )

                    with self.assertRaisesRegex(
                        APIError, "Malformed JSON-RPC response"
                    ):
                        await call.wait(timeout=0.1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
