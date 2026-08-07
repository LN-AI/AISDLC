def get_user(session, user_id):
    return session.query(User).filter(User.id == user_id).one_or_none()


def search(conn, name):
    return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchall()
