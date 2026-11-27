import uvicorn

import config

if __name__ == "__main__":
    s = config.settings()
    uvicorn.run("api.server:app", host=s.api_host, port=s.api_port, proxy_headers=True, forwarded_allow_ips="*")
