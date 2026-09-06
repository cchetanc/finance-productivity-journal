import streamlit as st
import extra_streamlit_components as stx

def get_cookie_manager():
    return stx.CookieManager(key="auth_cookies")

cm1 = get_cookie_manager()
cm2 = get_cookie_manager()
