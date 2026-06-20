import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key: str = os.environ.get('AUTH_SECRET_KEY', 'CHANGE_ME_IN_PRODUCTION')
    algorithm: str = 'HS256'
    # 重置密码后的默认密码
    reset_password: str = 'cloudbak@123'
    # 登录jwt有效期
    access_token_expire_minutes: int = 60 * 24 * 7
    # 用户登录错误次数ban
    user_fail_ban_count: int = 5
    # ip 地址登录错误次数ban
    ip_fail_ban_count: int = 10
    # 默认最大用户数，-1 为不限制
    max_user_count: int = 5

    class Config:
        env_prefix = 'AUTH_'
        env_file = ".env"
        env_file_encoding = 'utf-8'
        extra = 'allow'


settings = Settings()
