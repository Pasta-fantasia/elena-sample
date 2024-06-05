import pathlib
import time
from os import path

from elena.domain.model.bot_config import BotConfig
from elena.domain.model.bot_status import BotStatus, BotBudget
from elena.domain.ports.exchange_manager import ExchangeManager
from elena.domain.ports.logger import Logger
from elena.domain.ports.metrics_manager import MetricsManager
from elena.domain.ports.notifications_manager import NotificationsManager
from elena.domain.ports.strategy_manager import StrategyManager
from elena.domain.services.generic_bot import GenericBot

import numpy as np
import pandas as pd
import pandas_ta as ta


class Common_stop_loss_budget_control(GenericBot):
    # Strict dates DCA, just buy on a regular basis.

    spend_on_order: float
    lr_buy_longitude: float
    band_length: float
    band_mult: float
    band_low_pct: float
    minimal_benefit_to_start_trailing: float
    min_price_to_start_trailing: float

    _logger: Logger
    _metrics_manager: MetricsManager
    _notifications_manager: NotificationsManager

    def _spent_by_frequency(self, frequency="D", shift=None):
        if len(self.status.active_trades) > 0:
            df = pd.DataFrame([model.dict() for model in self.status.active_trades])
            df['entry_time'] = pd.to_datetime(df['entry_time'], unit='ms', utc=True)
            # https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.Grouper.html
            # https://pandas.pydata.org/pandas-docs/stable/user_guide/timeseries.html#offset-aliases
            if shift:
                df['entry_time'] = df['entry_time'] + pd.Timedelta(shift)
            return df.groupby(pd.Grouper(key='entry_time', freq=frequency)).agg({'entry_cost': 'sum'})
        else:
            df = pd.DataFrame(
                {
                    "entry_time": [pd.Timestamp.now(tz='UTC')],
                    "entry_cost": [0.0]
                }
            )
            return df

    def _spent_in_current_freq(self, frequency="D", spent_times_shift=None) -> float:
        real_spent_by_frequency = self._spent_by_frequency(frequency, spent_times_shift)

        now = pd.DataFrame(
            {
                "entry_time": [pd.Timestamp.now(tz='UTC')],
                "fake_entry_cost": [0.0]
            }
        )
        if spent_times_shift:
            now['entry_time'] = now['entry_time'] + pd.Timedelta(spent_times_shift)
        fake_spent_by_frequency = now.groupby(pd.Grouper(key='entry_time', freq=frequency)).agg({'fake_entry_cost': 'sum'})
        merged = real_spent_by_frequency.merge(fake_spent_by_frequency, on='entry_time', how='outer')

        spent = merged["entry_cost"][-1:].iloc[0]

        if np.isnan(spent):
            spent = 0.0
        else:
            spent = float(spent)

        return spent

    def budget_left_in_freq(self) -> float:
        if 'spent_times_shift' in self.bot_config.config:
            spent_times_shift = self.bot_config.config['spent_times_shift']
        else:
            spent_times_shift = None

        budget_left = self.status.budget.free
        if 'daily_budget' in self.bot_config.config:
            frequency = "D"
            daily_budget = self.bot_config.config['daily_budget']
            spent = self._spent_in_current_freq(frequency, spent_times_shift)
            daily_budget_left = daily_budget - spent
            budget_left = min(budget_left, daily_budget_left)

        if 'weekly_budget' in self.bot_config.config:
            frequency = "W"
            weekly_budget = self.bot_config.config['weekly_budget']
            spent = self._spent_in_current_freq(frequency, spent_times_shift)
            weekly_budget_left = weekly_budget - spent
            budget_left = min(budget_left, weekly_budget_left)

        if budget_left < 0.0:
            budget_left = 0.0

        return budget_left

    def _cancel_active_orders_with_lower_stop_loss(self, new_stop_loss: float) -> float:
        # Cancel any active stop order with a limit lower than the new one.
        # return the total amount of canceled orders
        total_amount_canceled_orders = 0
        canceled_orders = []
        for order in self.status.active_orders:
            if new_stop_loss > order.stop_price:
                cancelled_order = self.cancel_order(order.id)
                if cancelled_order:
                    total_amount_canceled_orders = total_amount_canceled_orders + order.amount
                    canceled_orders.append(order.id)
                else:
                    self._logger.error(f"Error canceling order: {order.id}.")
        return total_amount_canceled_orders, canceled_orders

    def manage_trailing_stop_losses(self, data: pd.DataFrame, estimated_close_price: float, band_length: float, band_mult: float):
        # TRAILING STOP LOGIC
        # Indicator: Standard Error Bands based on DEMA
        #   new_stop_loss

        sl_dema = ta.dema(close=data.Close, length=band_length)
        sl_stdev = ta.stdev(close=data.Close, length=band_length)
        sl_lower_band = sl_dema - (band_mult * sl_stdev)

        new_stop_loss = float(sl_lower_band[-1:].iloc[0])  # get the last
        self._metrics_manager.gauge("new_stop_loss", self.id, new_stop_loss, ["indicator"])

        #   stop_price
        stop_price = new_stop_loss * (1 - (self.band_low_pct / 100))
        self._metrics_manager.gauge("stop_price", self.id, stop_price, ["indicator"])

        if stop_price < new_stop_loss * 0.8:
            self._logger.error(
                f"price ({stop_price}) is too far from new_stop_loss({new_stop_loss}) it may happend on test envs.")
            new_stop_loss = 0
            stop_price = 0

        if new_stop_loss > estimated_close_price:
            self._logger.warning(
                f"new_stop_loss ({new_stop_loss}) should be never higher than last_close({estimated_close_price})")
            new_stop_loss = 0
            stop_price = 0

        total_amount_canceled_orders, canceled_orders = self._cancel_active_orders_with_lower_stop_loss(new_stop_loss)
        new_trades_on_limit_amount = 0

        # find trades that get the limit to start trailing stops
        for trade in self.status.active_trades:
            if trade.exit_order_id == '0':  # TODO exit_order_id
                if stop_price > trade.entry_price * (1 + (
                        self.minimal_benefit_to_start_trailing / 100)) and stop_price > self.min_price_to_start_trailing:
                    trade.exit_order_id = "new_grouped_order"
                    new_trades_on_limit_amount = new_trades_on_limit_amount + trade.size

        # create a new stop order with the sum of all canceled orders + the trades that enter the limit
        grouped_amount_canceled_orders_and_new_trades = total_amount_canceled_orders + new_trades_on_limit_amount

        if grouped_amount_canceled_orders_and_new_trades >= self.limit_min_amount():
            new_order = self.stop_loss(amount=grouped_amount_canceled_orders_and_new_trades, stop_price=new_stop_loss,
                                       price=stop_price)

            if new_order:
                canceled_orders.append("new_grouped_order")

                # update trades with the new_order_id
                for trade in self.status.active_trades:
                    if trade.exit_order_id in canceled_orders:
                        trade.exit_order_id = new_order.id
                        trade.exit_price = new_stop_loss  # not real until the stop loss really executes.
            else:
                # TODO
                self._logger.error("Can't create stop loss grouped_amount_canceled_orders_and_new_trades ")

    def init(self, manager: StrategyManager, logger: Logger, metrics_manager: MetricsManager, notifications_manager: NotificationsManager, exchange_manager: ExchangeManager, bot_config: BotConfig, bot_status: BotStatus, ):  # type: ignore
        super().init(manager, logger, metrics_manager, notifications_manager, exchange_manager, bot_config, bot_status,)

    def next(self) -> BotStatus:
        return self.status
