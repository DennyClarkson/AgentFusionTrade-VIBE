import json

from fusion.ai import AIClient
from fusion.config import DEFAULTS


def test_provider_cannot_reflect_secret_in_valid_patch_or_validation_location(monkeypatch):
    secret="fixture-provider-credential-123456"
    monkeypatch.setenv("FUSION_TEST_CREDENTIAL",secret)
    responses=[{"decision":"abstain","confidence":0,"summary":secret,"strategy_patch":{secret:1}},
               {"decision":"abstain","confidence":0,"summary":"invalid field",secret:"extra"}]
    class Response:
        status_code=200
        def json(self):return {"model":secret,"choices":[{"message":{"content":json.dumps(responses.pop(0))}}],"usage":{"prompt_tokens":1,"hidden":secret}}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,*args,**kwargs):return Response()
    monkeypatch.setattr("fusion.ai.httpx.Client",Client)
    config={**DEFAULTS["ai"],"key_env":"FUSION_TEST_CREDENTIAL","output_token_policy":"fixed"}
    for _ in range(2):
        result=AIClient().opinion("test",config,"test",{})
        assert secret not in json.dumps(result)
