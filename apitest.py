import requests

response = requests.post(
    "http://35.85.148.23:8000/api/v1/jobs/search",
    json={
        "query": "後端工程師",
        "location_code": None,
        "duty_code": None,
    },
)

print(response.status_code)
print(response.json())
