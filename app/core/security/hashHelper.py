from bcrypt import hashpw, checkpw,gensalt


class HashHelper(object):
    @staticmethod
    def hash_password(password: str) -> str:
        return hashpw(password.encode('utf-8'), gensalt()).decode('utf-8')
    
    @staticmethod
    def verify_password(password: str, hashed_password: str) -> bool:
        return checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))
