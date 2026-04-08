# In a python shell
import redis
import json
r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
data = r.get("proactive:risk:latest")
print(json.dumps(json.loads(data), indent=2))
