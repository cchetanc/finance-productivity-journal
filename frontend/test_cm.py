import streamlit as st
import extra_streamlit_components as stx
import uuid

def get_cookie_manager():
    return stx.CookieManager(key=f"auth_cookies_{uuid.uuid4().hex}")

cm = get_cookie_manager()
cm.set("test", "test")
st.write("Cookies:")
st.write(cm.get_all())
