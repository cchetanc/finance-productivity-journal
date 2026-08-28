import json
import asyncio
import os
import sys
import logging
from google.adk.agents import Agent, ParallelAgent
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

MCP_SERVER_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "finance_mcp_server.py")
)

# Initialize MCP Toolset
finance_mcp_toolset = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[MCP_SERVER_PATH],
        )
    )
)

MODEL_NAME = "gemini-2.5-flash"

# Define Domain Agents
equity_agent = Agent(
    name="equity_agent",
    model=MODEL_NAME,
    description="Analyzes equity and stock market implications. Use get_market_data for real-time ticker data.",
    instruction="""
        You are an expert Equity Analyst. 
        Focus on stock picks, valuations, sectors, and price trends.
        If the user mentions a stock or company, use the 'get_market_data' tool to get its current fundamentals.
        Provide a concise, professional analysis.
    """,
    tools=[finance_mcp_toolset],
    output_key="equity_insight"
)

mf_agent = Agent(
    name="mf_agent",
    model=MODEL_NAME,
    description="Analyzes mutual funds, SIPs, and asset allocation.",
    instruction="""
        You are an expert Mutual Fund Analyst. 
        Focus on SIPs, asset allocation, ETFs, and fund performance.
        If a specific fund or ETF is mentioned, use 'get_fund_data' to fetch its details.
        Provide a concise, professional analysis.
    """,
    tools=[finance_mcp_toolset],
    output_key="mf_insight"
)

commodity_agent = Agent(
    name="commodity_agent",
    model=MODEL_NAME,
    description="Analyzes commodity markets like gold, oil, silver, etc.",
    instruction="""
        You are an expert Commodity Analyst.
        Focus on precious metals, energy, and agricultural trends.
        Use 'get_market_data' (e.g. GC=F for Gold, CL=F for Crude Oil) if needed.
    """,
    tools=[finance_mcp_toolset],
    output_key="commodity_insight"
)

macro_agent = Agent(
    name="macro_agent",
    model=MODEL_NAME,
    description="Analyzes macroeconomic and liquidity implications.",
    instruction="""
        You are an expert Macroeconomist. 
        Focus on global liquidity, inflation, interest rates, and systemic risk.
        Use 'get_macro_indicators' to get current interest rates and inflation data.
    """,
    tools=[finance_mcp_toolset],
    output_key="macro_insight"
)

fixed_income_agent = Agent(
    name="fixed_income_agent",
    model=MODEL_NAME,
    description="Analyzes fixed income, bonds, and debt instruments.",
    instruction="""
        You are an expert Fixed Income Analyst.
        Focus on bond yields, treasury bills, corporate debt, and fixed returns.
        Use 'get_macro_indicators' to check treasury yields.
    """,
    tools=[finance_mcp_toolset],
    output_key="fixed_income_insight"
)

# Router and Synthesizer as standard GenerativeModels for speed and dynamic control
from google import genai
from google.genai import types

def get_client():
    return genai.Client(vertexai=True, project="gen-lang-client-0048936678", location="us-central1")

class Router:
    def __init__(self):
        self.client = get_client()
    
    async def generate_content_async(self, prompt):
        config = types.GenerateContentConfig(response_mime_type="application/json")
        return await self.client.aio.models.generate_content(
            model="gemini-2.5-flash-8b",
            contents=prompt,
            config=config
        )

class Synthesizer:
    def __init__(self):
        self.client = get_client()
        
    async def generate_content_async(self, prompt):
        config = types.GenerateContentConfig(
            system_instruction="You are the Chief Investment Officer (CIO) for a Chartered Financial Analyst (CFA) group. Synthesize expert insights into actionable advice."
        )
        return await self.client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config
        )

def get_router():
    return Router()

def get_synthesizer():
    return Synthesizer()

class Orchestrator:
    def __init__(self):
        self.agent_map = {
            "EQUITY": equity_agent,
            "MUTUAL_FUNDS": mf_agent,
            "COMMODITY": commodity_agent,
            "MACRO": macro_agent,
            "FIXED_INCOME": fixed_income_agent
        }
        
    async def process_query_async(self, query: str) -> str:
        # Step 1: Routing
        router_prompt = f"""
        Determine which expert agents are needed to answer the user query.
        Query: "{query}"
        Respond with a JSON array containing one or more of the following strings exactly: 
        "EQUITY", "MUTUAL_FUNDS", "COMMODITY", "MACRO", "FIXED_INCOME".
        Return ONLY the JSON array.
        """
        router = get_router()
        try:
            # We use sync generate_content here because it's fast, but ideally async
            route_resp = await router.generate_content_async(router_prompt)
            routes = json.loads(route_resp.text)
            if not isinstance(routes, list):
                routes = ["EQUITY", "MACRO"]
        except Exception as e:
            logging.error(f"Router parse failed: {e}")
            routes = ["EQUITY", "MACRO"]
            
        # Step 2: Dynamic Parallel Execution of Domain Agents
        # We manually construct the prompt and call Vertex AI for each selected agent in parallel
        # to avoid ADK Runner framework requirements in this FastAPI context.
        import asyncio
        from google import genai
        from google.genai import types
        
        async def run_domain_agent(agent_name: str):
            agent = self.agent_map.get(agent_name)
            if not agent:
                return f"[{agent_name}]: Not found."
            
            client = get_client()
            config = types.GenerateContentConfig(system_instruction=agent.instruction)
            
            try:
                resp = await client.aio.models.generate_content(
                    model=agent.model,
                    contents=query,
                    config=config
                )
                return f"[{agent.name} Insight]:\n{resp.text}"
            except Exception as e:
                return f"[{agent.name} Error]: {e}"
                
        if not routes:
            routes = ["EQUITY"]
            
        tasks = [run_domain_agent(r) for r in routes]
        results = await asyncio.gather(*tasks)
        combined_insights = "\n\n".join(results)
        # Step 3: Synthesize final answer
        if len(results) == 1:
            return results[0]

        synthesizer_prompt = f"""
        Based on the following expert insights, provide a single, cohesive, and actionable response to the user's query.
        Keep the response professional, concise, and structured. Do not use overly complex formatting if it is to be read aloud.
        
        User Query: "{query}"
        
        Expert Insights:
        {combined_insights}
        """
        
        synthesizer = get_synthesizer()
        final_answer = await synthesizer.generate_content_async(synthesizer_prompt)
        
        return final_answer.text
    def process_query(self, query: str) -> str:
        # Helper to run async from sync contexts
        return asyncio.run(self.process_query_async(query))
