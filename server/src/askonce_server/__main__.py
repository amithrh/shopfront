"""`python -m askonce_server` -> uvicorn on 0.0.0.0:8000 with MCP at /mcp."""

from askonce_server.app.run import main

if __name__ == "__main__":
    main()
