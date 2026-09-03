import streamlit as st

from auth_helper import login_widget
from trade_terminal_widget import render_trade_terminal
from ui_helpers import hide_streamlit_chrome, render_page_nav

BACKEND_URL = "https://finance-prod-app-backend-36680800010.asia-south1.run.app"

st.set_page_config(page_title="Trade Terminal", layout="wide", initial_sidebar_state="collapsed")
hide_streamlit_chrome()

if not login_widget(BACKEND_URL):
    st.stop()

render_page_nav()

st.markdown("""
<style>
.block-container { max-width: 760px !important; margin: 0 auto !important; }
</style>
""", unsafe_allow_html=True)

render_trade_terminal(BACKEND_URL, show_title=True)