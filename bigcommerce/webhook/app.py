from flask import Flask, request

app = Flask(__name__)

PORT = 5050


@app.route('/webhook/bigcommerce/uat/orders', methods=['POST'])
def orders_webhook():
    data = request.get_json(force=True, silent=True)
    print(data)
    return '', 200


if __name__ == '__main__':
    app.run(port=PORT)
