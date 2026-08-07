import logging

def on_login(user):
    logging.info("login ok user=%s ssn=%s card=%s", user.email, user.ssn, user.card_number)
