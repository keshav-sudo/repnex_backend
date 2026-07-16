from app.schemas.connection import _parse_connection_string
from app.services.connection_service import _build_mongo_uri

def test_parse_mongodb_srv_connection_string():
    # 1. mongodb+srv (Atlas style)
    cs = "mongodb+srv://nischalagarwal674_db_user:8N3I5G61gOjDylJU@cluster.x65blkq.mongodb.net/heliflow?retryWrites=true&w=majority&appName=Cluster"
    res = _parse_connection_string(cs)
    
    assert res["db_type"] == "mongodb"
    assert res["host"] == "cluster.x65blkq.mongodb.net"
    assert res["port"] == 0
    assert res["db_name"] == "heliflow"
    assert res["username"] == "nischalagarwal674_db_user"
    assert res["password"] == "8N3I5G61gOjDylJU"

def test_parse_mongodb_standard_connection_string():
    # 2. mongodb standard (with port)
    cs = "mongodb://myuser:mypassword@localhost:27017/mydatabase"
    res = _parse_connection_string(cs)
    
    assert res["db_type"] == "mongodb"
    assert res["host"] == "localhost"
    assert res["port"] == 27017
    assert res["db_name"] == "mydatabase"
    assert res["username"] == "myuser"
    assert res["password"] == "mypassword"

def test_build_mongo_uri_srv():
    # 1. SRV using host (.mongodb.net)
    uri = _build_mongo_uri("cluster.x65blkq.mongodb.net", 27017, "heliflow", "user", "pass")
    assert uri == "mongodb+srv://user:pass@cluster.x65blkq.mongodb.net/heliflow"
    
    # 2. SRV using port 0
    uri = _build_mongo_uri("my-custom-mongo-srv", 0, "db", "user", "pass")
    assert uri == "mongodb+srv://user:pass@my-custom-mongo-srv/db"

def test_build_mongo_uri_standard():
    # Standard mongodb with port
    uri = _build_mongo_uri("localhost", 27017, "mydatabase", "user", "pass")
    assert uri == "mongodb://user:pass@localhost:27017/mydatabase"
