import ccxt
import numpy as np
import pandas as pd
import time

from func_get import get_json, get_currency, get_bid_price, get_ask_price, get_last_price, get_base_currency_free, get_quote_currency_free, get_base_currency_value, get_quote_currency_value, get_order_fee, get_available_cash_flow, get_funding_payment
from func_cal import round_amount, cal_unrealised, cal_available_budget, cal_end_balance
from func_update import update_json, append_csv, append_order, remove_order, append_error_log, update_last_loop_price, update_transfer
from func_noti import noti_success_order, noti_warning, print_current_balance, print_hold_assets, print_pending_order


def cal_buy_price_list(remain_buy_orders, bid_price, open_orders_df_path, config_params):
    # Update open_orders_df before cal price.
    open_orders_df = pd.read_csv(open_orders_df_path)
    open_buy_orders_df = open_orders_df[open_orders_df['side'] == 'buy']
    open_sell_orders_df = open_orders_df[open_orders_df['side'] == 'sell']
    min_open_sell_price = min(open_sell_orders_df['price'], default=np.inf)

    if len(open_buy_orders_df) > 0:
        start_buy_price = min(open_buy_orders_df['price']) - config_params['grid']
    else:
        if len(open_sell_orders_df) == 0:
            start_buy_price = bid_price - (config_params['grid'] * config_params['start_safety'])
        else:
            # Use (grid * 2) to prevent dupplicate order.
            start_buy_price = min(min_open_sell_price - (config_params['grid'] * 2), bid_price)
    
    buy_price = min(start_buy_price, config_params['max_price'])
    buy_price_list = []

    while (buy_price > config_params['min_price']) & (len(buy_price_list) < remain_buy_orders):
        buy_price_list.append(buy_price)
        buy_price -= config_params['grid']

    return buy_price_list


def open_buy_orders_grid(exchange, config_params, last_loop_path, transfer_path, open_orders_df_path, transactions_df_path, error_log_df_path, cash_flow_df_path):
    cash_flow_df = pd.read_csv(cash_flow_df_path)
    open_orders_df = pd.read_csv(open_orders_df_path)
    last_loop = get_json(last_loop_path)
    transfer = get_json(transfer_path)

    base_currency, quote_currency = get_currency(config_params['symbol'])

    open_buy_orders_df = open_orders_df[open_orders_df['side'] == 'buy']
    open_sell_orders_df = open_orders_df[open_orders_df['side'] == 'sell']
    max_open_buy_price = max(open_buy_orders_df['price'], default=0)
    min_open_sell_price = min(open_sell_orders_df['price'], default=np.inf)
    available_cash_flow = get_available_cash_flow(transfer, cash_flow_df)
    quote_currency_free = get_quote_currency_free(exchange, quote_currency)
    available_budget = cal_available_budget(quote_currency_free, available_cash_flow, transfer)

    bid_price = get_bid_price(exchange, config_params['symbol'])
    print(f"Bid price: {bid_price} {quote_currency}")
    
    if min(bid_price, min_open_sell_price - config_params['grid']) - max_open_buy_price > config_params['grid']:
        cancel_open_buy_orders_grid(exchange, config_params, open_orders_df_path, transactions_df_path, error_log_df_path)
        n_open_buy_orders = 0
    else:
        n_open_buy_orders = len(open_buy_orders_df)

    if config_params['circuit_limit'] == 0:
        n_limit_order = np.inf
    else:    
        n_limit_order = config_params['circuit_limit']

    remain_buy_orders = max(n_limit_order - n_open_buy_orders, 0)
    buy_price_list = cal_buy_price_list(remain_buy_orders, bid_price, open_orders_df_path, config_params)
    

    print(f"Open {len(buy_price_list)} buy orders")
    
    for price in buy_price_list:
        amount = config_params['value'] / price
        amount = round_amount(amount, exchange, config_params['symbol'], type='down')

        if available_budget >= config_params['value']:
            buy_order = exchange.create_order(config_params['symbol'], 'limit', 'buy', amount, price, params={'postOnly':True})
            append_order(buy_order, 'amount', open_orders_df_path)
            print(f"Open buy {amount} {base_currency} at {price} {quote_currency}")

            quote_currency_free = get_quote_currency_free(exchange, quote_currency)
            available_budget = cal_available_budget(quote_currency_free, available_cash_flow, transfer)
        else:
            print(f"Error: Cannot buy at price {price} {quote_currency} due to insufficient fund!!!")
            break


def cal_sell_price(order, ask_price, config_params):
    sell_price = max(order['price'] + config_params['grid'], ask_price)

    return sell_price

    
def open_sell_orders_grid(buy_order, exchange, config_params, open_orders_df_path, error_log_df_path):
    base_currency, quote_currency = get_currency(config_params['symbol'])
    ask_price = get_ask_price(exchange, config_params['symbol'])
    sell_price = cal_sell_price(buy_order, ask_price, config_params)
    
    try:
        sell_amount = buy_order['filled']
        sell_order = exchange.create_order(config_params['symbol'], 'limit', 'sell', sell_amount, sell_price)
        append_order(sell_order, 'amount', open_orders_df_path)
    except (ccxt.InvalidOrder, ccxt.InsufficientFunds):
        # InvalidOrder: Filled with small amount before force closed.
        # InvalidOrder: The order has already been closed by postOnly param.
        # InvalidOrder: Exchange fail to update actual filled amount.
        # InsufficientFunds: Not available amount to sell (could caused by decimal).
        free_amount = get_base_currency_free(exchange, base_currency)
        sell_amount = round_amount(free_amount, exchange, config_params['symbol'], type='down')

        if sell_amount > 0:
            # Free amount more than minimum order, sell all.
            sell_order = exchange.create_order(config_params['symbol'], 'market', 'sell', sell_amount)
        else:
            sell_order = None

        append_error_log('CannotOpenSell', error_log_df_path)
    
    print(f"Open sell {sell_amount} {base_currency} at {sell_price} {quote_currency}")


def clear_orders_grid(side, exchange, bot_name, config_params, open_orders_df_path, transactions_df_path, error_log_df_path):
    open_orders_df = pd.read_csv(open_orders_df_path)
    open_orders_list = open_orders_df[open_orders_df['side'] == side]['order_id'].to_list()

    if side == 'sell':
        # Buy orders: FIFO.
        # Sell orders: LIFO.
        open_orders_list.reverse()
    
    for order_id in open_orders_list:
        order = exchange.fetch_order(order_id, config_params['symbol'])
        
        if order['status'] == 'closed':
            noti_success_order(order, bot_name, config_params['symbol'])

            if side == 'buy':
                open_sell_orders_grid(order, exchange, config_params, open_orders_df_path, error_log_df_path)

            remove_order(order_id, open_orders_df_path)
            append_order(order, 'filled', transactions_df_path)

        elif order['status'] == 'canceled':
            # Canceld by param PostOnly.
            remove_order(order_id, open_orders_df_path)


def cancel_open_buy_orders_grid(exchange, config_params, open_orders_df_path, transactions_df_path, error_log_df_path):
    open_orders_df = pd.read_csv(open_orders_df_path)
    open_buy_orders_df = open_orders_df[open_orders_df['side'] == 'buy']
    open_buy_orders_list = open_buy_orders_df['order_id'].to_list()
    
    if len(open_buy_orders_list) > 0:
        for order_id in open_buy_orders_list:
            order = exchange.fetch_order(order_id, config_params['symbol'])
            
            try:
                exchange.cancel_order(order_id)
                print(f"Cancel order {order_id}")
                
                if order['filled'] > 0:
                    append_order(order, 'filled', transactions_df_path)
                    open_sell_orders_grid(order, exchange, config_params, open_orders_df_path, error_log_df_path)
                
                remove_order(order_id, open_orders_df_path)
            except ccxt.OrderNotFound:
                # No order in the system (could casued by the order is queued), skip for the next loop.
                append_error_log('OrderNotFound', error_log_df_path)
                print(f"Error: Cannot cancel order {order_id} due to unavailable order!!!")
            except ccxt.InvalidOrder:
                # The order is closed by system (could caused by post_only param for buy orders).
                append_error_log(f'InvalidOrder', error_log_df_path)
                remove_order(order_id, open_orders_df_path)


def check_circuit_breaker(exchange, bot_name, config_system, config_params, last_loop_path, open_orders_df_path, transactions_df_path, error_log_df_path):
    cont_flag = 1

    if config_params['circuit_limit'] == 0:
        circuit_limit = np.inf
    else:
        circuit_limit = config_params['circuit_limit']
    
    _, quote_currency = get_currency(config_params['symbol'])
    last_loop = get_json(last_loop_path)
    transactions_df = pd.read_csv(transactions_df_path)
    update_last_loop_price(exchange, config_params['symbol'], last_loop_path)

    if len(transactions_df) >= circuit_limit:
        side_list = transactions_df['side'][-circuit_limit:].unique()
        last_price = get_last_price(exchange, config_params['symbol'])

        if (len(side_list) == 1) & (side_list[0] == 'buy') & (last_price <= last_loop['price']):
            cancel_open_buy_orders_grid(exchange, config_params, open_orders_df_path, transactions_df_path, error_log_df_path)
            noti_warning(f"Circuit breaker at {last_price} {quote_currency}", bot_name)
            time.sleep(config_system['idle_rest'])

    return cont_flag
        

def update_loss(loss, last_loop_path):
    last_loop = get_json(last_loop_path)
    last_loop['loss'] -= loss

    update_json(last_loop, last_loop_path)


def reset_loss(last_loop_path):
    last_loop = get_json(last_loop_path)
    last_loop['loss'] = 0

    update_json(last_loop, last_loop_path)


def cut_loss(exchange, bot_name, config_system, config_params, last_loop_path, open_orders_df_path, error_log_df_path, withdraw_flag):
    open_orders_df = pd.read_csv(open_orders_df_path)
    max_sell_price = max(open_orders_df['price'])
    canceled_df = open_orders_df[open_orders_df['price'] == max_sell_price]

    _, quote_currency = get_currency(config_params['symbol'])

    canceled_id = canceled_df['order_id'].reset_index(drop=True)[0]
    buy_amount = canceled_df['amount'].reset_index(drop=True)[0]
    buy_price = max_sell_price - config_params['grid']
    buy_value = buy_price * buy_amount

    try:
        exchange.cancel_order(canceled_id)
        time.sleep(config_system['idle_stage'])
        canceled_order = exchange.fetch_order(canceled_id, config_params['symbol'])

        while canceled_order['status'] != 'canceled':
            # Cancel orders will be removed from db on the next loop by check_orders_status.
            time.sleep(config_system['idle_stage'])
            canceled_order = exchange.fetch_order(canceled_id, config_params['symbol'])

        remove_order(canceled_id, open_orders_df_path)

        sell_order = exchange.create_order(config_params['symbol'], 'market', 'sell', buy_amount)
        time.sleep(config_system['idle_stage'])

        while sell_order['status'] != 'closed':
            time.sleep(config_system['idle_stage'])
            sell_order = exchange.fetch_order(sell_order['id'], config_params['symbol'])
            
        fee = get_order_fee(sell_order, exchange, config_params['symbol'], config_system)
        new_sell_amount = sell_order['amount']
        new_sell_price = sell_order['price']
        new_sell_value = new_sell_amount * new_sell_price
        loss = new_sell_value - buy_value + fee
        
        update_loss(loss, last_loop_path)
        noti_warning(f"Cut loss {loss} {quote_currency} at {new_sell_price} {quote_currency}", bot_name)

        if withdraw_flag == False:
            time.sleep(config_system['idle_rest'])
    
    except ccxt.InvalidOrder:
        # Order has already been canceled from last loop but failed to update df.
        append_error_log(f'InvalidOrder:LastLoopClose', error_log_df_path)
        remove_order(canceled_id, open_orders_df_path)


def update_value(config_params, config_params_path):
    max_n_order = (config_params['max_price'] - config_params['min_price']) / config_params['grid']
    config_params['value'] = config_params['budget'] / max_n_order

    update_json(config_params, config_params_path)


def update_end_date_grid(prev_date, exchange, bot_name, config_system, config_params, config_params_path, last_loop_path, transfer_path, open_orders_df_path, transactions_df_path, error_log_df_path, cash_flow_df_path):
    open_orders_df = pd.read_csv(open_orders_df_path)
    transactions_df = pd.read_csv(transactions_df_path)
    cash_flow_df = pd.read_csv(cash_flow_df_path)
    last_loop = get_json(last_loop_path)

    last_price = get_last_price(exchange, config_params['symbol'])
    base_currency, quote_currency = get_currency(config_params['symbol'])
    base_currency_free = get_base_currency_free(exchange, base_currency)
    quote_currency_free = get_quote_currency_free(exchange, quote_currency)

    last_transactions_df = transactions_df[pd.to_datetime(transactions_df['timestamp']).dt.date == prev_date]
    last_sell_df = last_transactions_df[last_transactions_df['side'] == 'sell']
    cash_flow = sum(last_sell_df['amount'] * config_params['grid'])
    funding_payment, _ = get_funding_payment(exchange, range='end_date')
    net_cash_flow = cash_flow - funding_payment

    transfer = get_json(transfer_path)
    net_transfer = transfer['deposit'] - transfer['withdraw']

    if net_transfer != 0:
        cancel_open_buy_orders_grid(exchange, config_params, open_orders_df_path, transactions_df_path, error_log_df_path)
    
    available_cash_flow = get_available_cash_flow(transfer, cash_flow_df)
    available_cash_flow += cash_flow

    # Cut loss until quote_currency_free is enough to withdraw.
    while quote_currency_free - available_cash_flow < -net_transfer:
        cut_loss(exchange, bot_name, config_system, config_params, last_loop_path, open_orders_df_path, error_log_df_path, withdraw_flag=True)
        quote_currency_free = get_quote_currency_free(exchange, quote_currency)

    current_value = get_base_currency_value(last_price, exchange, config_params['symbol'])
    cash = get_quote_currency_value(exchange, quote_currency)
    end_balance = cal_end_balance(current_value, cash, transfer)
    unrealised, _, _, _ = cal_unrealised(last_price, config_params['grid'], open_orders_df)

    cash_flow_list = [
        prev_date,
        config_params['grid'],
        config_params['value'],
        config_params['budget'],
        end_balance,
        unrealised,
        last_loop['loss'],
        cash_flow,
        funding_payment,
        net_cash_flow,
        base_currency_free,
        transfer['deposit'],
        transfer['withdraw'],
        transfer['withdraw_cash_flow'],
        available_cash_flow
        ]

    append_csv(cash_flow_list, cash_flow_df, cash_flow_df_path)
    reset_loss(last_loop_path)
    update_value(config_params, config_params_path)
    update_transfer(config_system['taker_fee_percent'], transfer_path)


def print_report_grid(exchange, config_params, open_orders_df_path):
    base_currency, quote_currency = get_currency(config_params['symbol'])
    last_price = get_last_price(exchange, config_params['symbol'])
    
    print_current_balance(last_price, exchange, config_params['symbol'])
    print(f"Last price: {last_price} {quote_currency}")
    print_hold_assets(last_price, base_currency, quote_currency, config_params['grid'], open_orders_df_path)
    print_pending_order(quote_currency, open_orders_df_path)