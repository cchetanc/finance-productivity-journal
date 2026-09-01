import scitreamlit as st

from trade_terminal_widget import render_trade_terminal

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Trade Terminal", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: 'Inter', sans-serif !important;
    background-color: #15120e !important;
    color: #e8ddc7 !important;
}
header[data-testid="stHeader"], footer, #MainMenu { display: none !important; }
section[data-testid="stSidebar"] { display: none !important; }
.block-container { max-width: 760px !important; margin: 0 auto !important; padding: 2rem 26px 3rem 26px !important; }
</style>
""", unsafe_allow_html=True)

render_trade_terminal(BACKEND_URL, show_title=True)