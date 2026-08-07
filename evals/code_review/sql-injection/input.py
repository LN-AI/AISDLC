def get_user(conn, user_id):
    query = "SELECT * FROM users WHERE id = " + user_id
    return conn.execute(query).fetchone()
