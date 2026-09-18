"""私有文件的原子写入，供证书及配置存储共享。"""
import os
import secrets

def private_write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp-'+secrets.token_hex(6))
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as file:file.write(value)
    os.replace(temp,path)

