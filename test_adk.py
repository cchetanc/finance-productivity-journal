import asyncio
from google.adk.agents import Agent

a = Agent(name='test', model='gemini-2.5-flash', instruction='say hello')

async def main():
    async for chunk in a.run_async('hi'):
        print("CHUNK:", chunk)

asyncio.run(main())
