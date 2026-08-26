# Google Cloud Codelabs Learnings Reference

This document serves as an architectural blueprint and reference knowledge base extracted from the target Google Cloud Gen AI Academy Codelabs.

---

## 1. Codelab 1: Deploy a RAG AI Agent in Streamlit using Google ADK and Cloud Run

### 1.1 Introduction
In this codelab, you will build an interactive AI Barista agent for a coffee shop. Using Google's open-source Agent Development Kit (ADK) and the Gemini 3.5 Flash model, you'll implement Retrieval-Augmented Generation (RAG) to ground the agent's recommendations in a mock menu dataset. Finally, you'll wrap the agent in a Streamlit user interface and deploy it to Cloud Run.

### 1.2 Scope & Actions
* **Data Layer**: Create a RAG data source (`menu.json`) containing coffee items, tags, and allergens.
* **Agent Build**: Build an AI agent using the ADK `LlmAgent` and connect a Python tool to load the menu data.
* **Interface Layer**: Wrap the agent in a Streamlit chat application that manages conversation history.
* **Deployment**: Deploy the Streamlit app to Cloud Run using source-based deployment.
* **Testing**: Test RAG grounding and allergen awareness.

### 1.3 Prerequisites
* A web browser such as Chrome.
* A Google Cloud project with billing enabled.
* Basic familiarity with Python.

### 1.4 Production Architectural Key Takeaways
* **ADK `LlmAgent` Grounding**: Use the open-source Agent Development Kit to enforce structured tool call flows.
* **Stateless Chat State**: Session context must be explicitly managed within the presentation layer (Streamlit/React) before sending back to serverless contexts.

---

## 2. Codelab 2: Build and Deploy AI Agents with Gemma 4 and BigQuery MCP Server in Cloud Run

### 2.1 Introduction
* **Note**: This feature is subject to the "Pre-GA Offerings Terms" in the General Service Terms section of the Service Specific Terms. Pre-GA features are available "as is" and might have limited support.

### 2.2 Scope & Actions
* **Model Hosting**: Deploy a Gemma 4 model on a Cloud Run RTX 6000 Pro GPU with vLLM.
* **Agent Integration**: Create an AI Agent using Agent Development Kit (ADK) and use Gemma 4 with it.
* **Structured Data**: Give AI Agents access to structured data in BigQuery using BigQuery MCP server.

### 2.3 System Stack Definitions
* **Gemma 4**: A family of Apache 2-licensed open weight models from Google DeepMind offering multimodal, multilingual reasoning, and an efficient architecture.
* **Cloud Run**: A serverless environment for containers with support for GPUs.
* **Agent Development Kit (ADK)**: An open-source agent development framework that lets you build, debug, and deploy reliable AI agents at enterprise scale.
* **BigQuery**: A fully managed, serverless enterprise data warehouse that allows you to store, query, and analyze massive datasets.
* **Model Context Protocol (MCP)**: Standardizes how large language models (LLMs) and AI applications or agents connect to external data sources. MCP servers let you use their tools, resources, and prompts to take actions and get updated data from their backend service. 
* **BigQuery MCP Server**: Gives AI agents a direct, secure way to analyze data in BigQuery. This fully managed MCP server removes management overhead, enabling you to focus on developing intelligent agents.

### 2.4 Production Architectural Key Takeaways
* **Standardized Interfaces (MCP)**: Use Model Context Protocol wrappers to expose data sources safely instead of hardcoding raw query executors.

---

## 3. Codelab 3: Run a Personal Agent on a Cloud Run Service (Coffee Shop Manager Assistant)

### 3.1 Introduction
* **Note**: This feature is subject to the "Pre-GA Offerings Terms" in the General Service Terms section of the Service Specific Terms. Pre-GA features are available "as is" and might have limited support.

### 3.2 Overview
In this codelab, you will build a personal AI assistant that helps you analyze business data and perform other tasks through a chat UI. You will use a Cloud Run service to host your personal agent.

Your agent will use Cloud Run sandboxes. Cloud Run sandboxes are a native, secure, and ultra-fast runtime environment built specifically for executing untrusted code and agent workloads, starting in milliseconds. The sandbox allows your AI Agent to dynamically write, run, and test code on the fly to solve complex analytical problems.

### 3.3 Runtime Context Separation
To ensure a seamless development experience when running locally versus in production:
* **In Production (Cloud Run sandbox)**: The agent runs code securely inside an isolated, containerized playground via a dedicated sandbox binary (`/usr/local/gcp/bin/sandbox`).
* **Locally (Your Machine)**: When running locally, the app detects that the production sandbox environment isn't present (`IS_LOCAL_MODE = True`). The agent executes Python scripts and shell commands directly on your local host machine's system terminal.

### 3.4 Scope & Actions
* **Scenario Context**: Manage a coffee shop preparing for a graduation weekend. Cross-reference raw Point-of-Sale (POS) data with the university's ceremony schedule to uncover hidden operational bottlenecks.
* **Sandbox Execution**: The agent uses a secure sandbox to write and execute Python scripts, analyzing drink complexity versus cashier headcount to recommend staffing and inventory adjustments.
* **UI & Workflows**: The agent pings the owner via a mock chat UI with targeted recommendations and waits for explicit permission before updating a spreadsheet with operational tasks.

### 3.5 Learnings Matrix
* How to create a Cloud Run service.
* How to deploy an ADK agent on a Cloud Run service.
* How to have an agent run code in a sandbox within a Cloud Run service.
* How to create a chat UI using WebSockets to interact with the background agent.

### 3.6 Production Architectural Key Takeaways
* **Code Execution Guardrails**: Untrusted code or dynamically generated scripts from agents must be isolated inside sandboxed runtimes like `/usr/local/gcp/bin/sandbox`.
* **State Operations via WebSockets**: Real-time multi-step processing requires persistent connection architectures (WebSockets) to decouple blocking computational workloads from standard API request lifecycles.
