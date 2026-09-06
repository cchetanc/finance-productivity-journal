# Frontend Architecture

The frontend of the Finance Intelligence Journal is designed to be an interactive, responsive, and visually appealing dashboard for managing personal finances and interacting with AI agents.

## Core Technologies
- **Framework:** Streamlit (Python)
- **Styling:** Custom CSS and Markdown injection for rich UI elements (e.g., chat bubbles, custom colors).
- **Client-Side Logic:** Vanilla JavaScript injected via Streamlit components for specific interactions (e.g., clock updates, scrolling).
- **Authentication:** Firebase Authentication.
- **Deployment:** Google Cloud Run.

## Architectural Components

1. **Streamlit App Structure (`app.py` & `pages/`):**
   - The application uses Streamlit's multipage architecture.
   - `app.py`: The main entry point handling global state, authentication checks, and the Daily Assistant chat interface.
   - `pages/`: Contains separate modules for specific features like Portfolio Management, Market Analysis, and the Trade Terminal.

2. **State Management (`st.session_state`):**
   - Manages user sessions, authentication tokens, UI toggle states, and temporary chat histories to ensure a seamless experience across page reloads.

3. **UI Helpers & Custom Components (`ui_helpers.py`):**
   - A collection of utility functions to inject custom CSS/JS.
   - Handles the rendering of styled chat bubbles, dynamic timestamps (using `Intl.DateTimeFormat` for IST), and trade terminal widgets.

4. **API Integration Layer:**
   - Uses the `requests` library to communicate with the FastAPI backend.
   - Handles asynchronous operations and timeouts (e.g., 180s timeout for complex agent tasks).
   - Formats payloads (including multi-turn chat history) for backend consumption.

## User Interaction Flow (Daily Assistant)
1. The user inputs a message in the Streamlit chat input.
2. The frontend appends the message to `st.session_state.messages` and renders the user bubble.
3. The frontend extracts the last few turns (e.g., last 6) and sends them to the backend API (`/api/market/voice`).
4. While waiting, a spinner is displayed.
5. Upon receiving the backend response, it is appended to the session state and rendered with custom formatting.
