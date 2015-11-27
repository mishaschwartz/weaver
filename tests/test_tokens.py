from nose.tools import ok_, assert_raises

from datetime import timedelta

from twitcher.utils import now
from twitcher.tokens import AccessToken

def test_access_token():
    creation_time = now()
    
    token = AccessToken(token='abcdef', creation_time=creation_time)
    ok_(token.not_before() == creation_time)
    ok_(token.not_after() > creation_time)
    ok_(token.is_valid() == True)

def test_bad_access_token():
    with assert_raises(TypeError) as e:
        AccessToken()
    with assert_raises(TypeError) as e:
        AccessToken(token='12345')

def test_invalid_access_token():
    creation_time = now() - timedelta(hours=2)
    
    token = AccessToken(token='abcdef', creation_time=creation_time)
    ok_(token.not_before() == creation_time)
    ok_(token.not_after() > creation_time)
    ok_(token.is_valid() == False)


def test_access_token_with_user_environ():
    creation_time = now()
    token = AccessToken(token='12345', creation_time=creation_time,
                        user_environ={'oauth_token': 'bfghk'})
    ok_(token.user_environ == {'oauth_token': 'bfghk'})
