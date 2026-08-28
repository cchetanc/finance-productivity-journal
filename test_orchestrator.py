import asyncio
from backend.app.agents import Orchestrator

async def test_orchestrator():
    o = Orchestrator()
    print("Testing 'What are the current macroeconomic risks and how will it affect AAPL?'")
    result = await o.process_query_async("What are the current macroeconomic risks and how will it affect AAPL?")
    print("RESULT:\n", result)

if __name__ == "__main__":
    asyncio.run(test_orchestrator())
