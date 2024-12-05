import logging
import requests
import json
from flask import Flask, request, jsonify, render_template, send_file, after_this_request
from neo4j import GraphDatabase
from neo4j.graph import Node, Relationship
import time
import os

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.DEBUG)

uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")  # Default to localhost if not set
user = os.getenv("NEO4J_USERNAME", "neo4j")           # Default to 'neo4j' user if not set
password = os.getenv("NEO4J_PASSWORD", "123456789")   # Default to your existing password if not set

# Connect to Neo4j
driver = GraphDatabase.driver(uri, auth=(user, password))
ETHERSCAN_API_KEY = 'WR981RAMB5X58PXCC7Y3NUYVU6FZ9XG19V'

def serialize_node(node):
    display_value = ""
    if 'proxyAddress' in node:
        display_value = node['proxyAddress'][:6] + '...' + node['proxyAddress'][-4:]
    elif 'contract_address' in node:
        display_value = node['contract_address'][:6] + '...' + node['contract_address'][-4:]
    else:
        display_value = str(node.id)

    properties = dict(node)
    for key, value in properties.items():
        if isinstance(value, float) and (value != value):  # Check for NaN
            properties[key] = None

    return {
        'id': node.id,
        'labels': list(node.labels),
        'properties': properties,
        'display': display_value
    }


def serialize_relationship(rel):
    """Helper function to serialize a Neo4j Relationship object."""
    properties = dict(rel)
    for key, value in properties.items():
        if isinstance(value, float) and (value != value):  # Check for NaN
            properties[key] = None
    
    return {
        'id': rel.id,
        'type': rel.type,
        'startNode': rel.start_node.id,
        'endNode': rel.end_node.id,
        'properties': properties
    }

def retrieve_source_codes(address: str):
    if len(address) != 42:
        return None

    url = f"https://api.etherscan.io/api?module=contract&action=getsourcecode&address={address}&apikey={ETHERSCAN_API_KEY}"
    retries = 3
    for attempt in range(retries):
        try:
            response = requests.get(url)
            data = response.json()
            if data["status"] != "1":
                return None
            
            info = data["result"][0]
            sourcecode = info['SourceCode']
            contract_name = info['ContractName']
            files = []
            try:
                contract_object = json.loads(sourcecode[1:-1])
                files = [(name, code['content']) for name, code in contract_object.get('sources', {}).items()]
                root = files[0][0]  # the file at index 0 is the root
            except json.JSONDecodeError:
                lines = sourcecode.splitlines()
                indices = [i for i, line in enumerate(lines) if line.startswith("// Dependency file:") or line.startswith("// Root file:")]
                files = [(lines[start].split(":")[1].strip(), '\n'.join(lines[start+1:end])) for start, end in zip(indices, indices[1:] + [len(lines)])]
                if len(files) == 0:
                    files = [(f"{contract_name}.sol", sourcecode)]
                    root = contract_name
                root = files[-1][0]  # the last file should be the root file
            return {
                'root': root.replace(" ", "_").replace("\\", "/").replace(".sol", "").split("/")[-1],
                'contract_name': contract_name,
                'files': files
            }
        except requests.RequestException as e:
            print(f"Error: Failed to retrieve source code for address {address}. Exception: {e}")
            if attempt < retries - 1:
                time.sleep(1)
            else:
                return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/query', methods=['POST'])
def query():
    cypher_query = request.json.get('query')
    limit = request.json.get('limit', 25)
    if not cypher_query.strip().upper().startswith('MATCH'):
        return jsonify({"error": "Only MATCH queries are allowed."}), 400

    cypher_query = f"{cypher_query} LIMIT {limit}"
    with driver.session() as session:
        result = session.run(cypher_query)
        nodes = []
        relationships = []
        for record in result:
            for value in record.values():
                if isinstance(value, Node):
                    nodes.append(serialize_node(value))
                elif isinstance(value, Relationship):
                    relationships.append(serialize_relationship(value))
        response = {'nodes': nodes, 'relationships': relationships}
    return jsonify(response)

@app.route('/contracts', methods=['GET'])
def get_contracts():
    limit = int(request.args.get('limit', 25))
    with driver.session() as session:
        result = session.run(f"MATCH (sc:SmartContractVersion) RETURN sc ORDER BY rand() LIMIT {limit}")
        data = [serialize_node(record["sc"]) for record in result]
    return jsonify({'nodes': data, 'relationships': []})

@app.route('/proxies', methods=['GET'])
def get_proxies():
    limit = int(request.args.get('limit', 25))
    with driver.session() as session:
        result = session.run(f"MATCH (p:Proxy) RETURN p ORDER BY rand() LIMIT {limit}")
        data = [serialize_node(record["p"]) for record in result]
    return jsonify({'nodes': data, 'relationships': []})

@app.route('/rootcauses', methods=['GET'])
def get_rootcauses():
    logging.debug("Fetching OBSERVED_CHANGES")
    limit = int(request.args.get('limit', 25))
    with driver.session() as session:
        result = session.run(f"MATCH (c:Change) RETURN c ORDER BY rand() LIMIT {limit}")
        data = [serialize_node(record["c"]) for record in result]
    logging.debug(f"OBSERVED_CHANGES fetched: {data}")
    return jsonify({'nodes': data, 'relationships': []})

@app.route('/proxy_contracts', methods=['GET'])
def get_proxy_contracts():
    proxy_address = request.args.get('proxy_address')
    if not proxy_address:
        return jsonify({"error": "Proxy address is required."}), 400
    
    limit = int(request.args.get('limit', 25))
    query = f"""
    MATCH (p:Proxy {{address: '{proxy_address}'}})-[r:IMPLEMENT]->(c:SmartContractVersion)
    RETURN p, r, c LIMIT {limit}
    """
    with driver.session() as session:
        result = session.run(query)
        nodes = []
        relationships = []
        node_ids = set()
        for record in result:
            p_node = serialize_node(record['p'])
            c_node = serialize_node(record['c'])
            r_rel = serialize_relationship(record['r'])
            if p_node['id'] not in node_ids:
                nodes.append(p_node)
                node_ids.add(p_node['id'])
            if c_node['id'] not in node_ids:
                nodes.append(c_node)
                node_ids.add(c_node['id'])
            relationships.append(r_rel)
    response = {'nodes': nodes, 'relationships': relationships}
    return jsonify(response)


@app.route('/implems_relationships', methods=['GET'])
def get_implems_relationships():
    try:
        logging.debug("Fetching IMPLEMENT relationships")
        limit = int(request.args.get('limit', 30))
        query = f"""
        MATCH p=()-[r:IMPLEMENT]->()
        RETURN p LIMIT {limit}
        """
        with driver.session() as session:
            result = session.run(query)
            nodes = []
            relationships = []
            node_ids = set()
            for record in result:
                if record['p'] is not None:
                    for node in record['p'].nodes:
                        if node.id not in node_ids:
                            nodes.append(serialize_node(node))
                            node_ids.add(node.id)
                    for rel in record['p'].relationships:
                        relationships.append(serialize_relationship(rel))
        response = {'nodes': nodes, 'relationships': relationships}
        logging.debug(f"IMPLEMENT relationships fetched: {response}")
        return jsonify(response)
    except Exception as e:
        logging.error(f"Error fetching IMPLEMENT relationships: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/observed_changes', methods=['GET'])
def get_observed_changes():
    try:
        logging.debug("Fetching OBSERVED_CHANGES relationships")
        limit = int(request.args.get('limit', 25))
        query = f"""
        MATCH p=()-[r:OBSERVED_CHANGES]->()
        RETURN p ORDER BY rand() LIMIT {limit}
        """
        with driver.session() as session:
            result = session.run(query)
            nodes = []
            relationships = []
            node_ids = set()
            for record in result:
                if record['p'] is not None:
                    for node in record['p'].nodes:
                        if node.id not in node_ids:
                            nodes.append(serialize_node(node))
                            node_ids.add(node.id)
                    for rel in record['p'].relationships:
                        relationships.append(serialize_relationship(rel))
        response = {'nodes': nodes, 'relationships': relationships}
        logging.debug(f"OBSERVED_CHANGES relationships fetched: {response}")
        return jsonify(response)
    except Exception as e:
        logging.error(f"Error fetching OBSERVED_CHANGES relationships: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/etherscan_contract/<address>', methods=['GET'])
def get_etherscan_contract(address):
    retries = 3
    for attempt in range(retries):
        try:
            contract_details_url = f"https://api.etherscan.io/api?module=contract&action=getsourcecode&address={address}&apikey={ETHERSCAN_API_KEY}"
            contract_details_response = requests.get(contract_details_url)
            if contract_details_response.status_code == 200:
                contract_details = contract_details_response.json()
                if contract_details['status'] == "1" and contract_details['message'] == "OK":
                    return jsonify({'contract_details': contract_details})
                else:
                    return jsonify({"error": "Unable to fetch contract details from Etherscan"}), 400
            else:
                return jsonify({"error": "Unable to fetch contract details from Etherscan"}), 400
        except requests.RequestException as e:
            logging.error(f"Error fetching contract details: {e}")
            if attempt < retries - 1:
                time.sleep(1)
            else:
                return jsonify({"error": f"Failed to retrieve contract details after {retries} attempts: {str(e)}"}), 500

@app.route('/search_graph', methods=['GET'])
def search_graph():
    search_term = request.args.get('query', '')
    if not search_term:
        return jsonify({"error": "Search term is required"}), 400

    query = f"""
    MATCH (n)
    WHERE ANY(prop in keys(n) WHERE n[prop] CONTAINS '{search_term}')
    RETURN n LIMIT 25
    """
    with driver.session() as session:
        result = session.run(query)
        nodes = []
        relationships = []
        node_ids = set()
        for record in result:
            node = serialize_node(record['n'])
            nodes.append(node)
            node_ids.add(node['id'])
            rel_query = f"MATCH (n)-[r]->(m) WHERE ID(n)={node['id']} RETURN r, m"
            rel_result = session.run(rel_query)
            for rel_record in rel_result:
                rel = serialize_relationship(rel_record['r'])
                if rel['endNode'] not in node_ids:
                    nodes.append(serialize_node(rel_record['m']))
                    node_ids.add(rel['endNode'])
                relationships.append(rel)
    return jsonify({'nodes': nodes, 'relationships': relationships})

@app.route('/filter_graph', methods=['GET'])
def filter_graph():
    age = request.args.get('age')
    transactions = request.args.get('transactions')
    # Implement filter logic based on age and number of transactions
    with driver.session() as session:
        query = f"""
        MATCH (c:Contract)
        WHERE c.age <= {age} AND c.totalTransactions >= {transactions}
        RETURN c LIMIT 25
        """
        result = session.run(query)
        data = [serialize_node(record["c"]) for record in result]
    return jsonify({"nodes": data, "relationships": []})

@app.route('/export_graph', methods=['POST'])
def export_graph():
    try:
        data = request.json
        node_ids = data.get('node_ids', [])
        relationship_ids = data.get('relationship_ids', [])

        nodes = []
        relationships = []

        with driver.session() as session:
            if node_ids:
                nodes_query = f"MATCH (n) WHERE id(n) IN {node_ids} RETURN n"
                nodes_result = session.run(nodes_query)
                nodes = [serialize_node(record["n"]) for record in nodes_result]
            
            if relationship_ids:
                rel_query = f"MATCH ()-[r]->() WHERE id(r) IN {relationship_ids} RETURN r"
                rel_result = session.run(rel_query)
                relationships = [serialize_relationship(record["r"]) for record in rel_result]

        graph_data = {"nodes": nodes, "relationships": relationships}
        file_path = '/tmp/graph_data.json'
        with open(file_path, 'w') as f:
            json.dump(graph_data, f)
        
        @after_this_request
        def remove_file(response):
            try:
                os.remove(file_path)
            except Exception as e:
                logging.error(f"Error removing file: {e}")
            return response

        return send_file(file_path, as_attachment=True, download_name='graph_data.json')
    except Exception as e:
        logging.error(f"Error exporting graph: {e}")
        return jsonify({"error": f"Failed to export graph: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
    # app.run(port=5001, debug=True)


