"""
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from datetime import datetime

from stream_alert.rule_promotion import LOGGER
from stream_alert.rule_promotion.publisher import StatsPublisher
from stream_alert.rule_promotion.statistic import StagingStatistic
from stream_alert.shared.athena import AthenaClient
from stream_alert.shared.config import load_config
from stream_alert.shared.rule_table import RuleTable


class RulePromoter(object):
    """Run queries to generate statistics on alerts."""

    ATHENA_S3_PREFIX = 'athena_partition_refresh'
    STREAMALERT_DATABASE = '{}_streamalert'

    def __init__(self):
        config = load_config()
        prefix = config['global']['account']['prefix']

        # Create the rule table class for getting staging information
        self._rule_table = RuleTable('{}_streamalert_rules'.format(prefix))

        athena_config = config['lambda']['athena_partition_refresh_config']

        # Get the name of the athena database to access
        db_name = athena_config.get('database_name', self.STREAMALERT_DATABASE.format(prefix))

        # Get the S3 bucket to store Athena query results
        results_bucket = athena_config.get(
            'results_bucket',
            's3://{}.streamalert.athena-results'.format(prefix)
        )

        self._athena_client = AthenaClient(db_name, results_bucket, self.ATHENA_S3_PREFIX)
        self._current_time = datetime.utcnow()

        # Store the SNS topic arn to send alert stat information to
        self._publisher = StatsPublisher(config, self._athena_client, self._current_time)

        self._staging_stats = dict()

    def _get_staging_info(self):
        """Query the Rule table for rule staging info needed to count each rule's alerts

        Example of rule metadata returned by RuleTable.remote_rule_info():
        {
            'example_rule_name':
                {
                    'Staged': True
                    'StagedAt': datetime.datetime object,
                    'StagedUntil': '2018-04-21T02:23:13.332223Z'
                }
        }
        """
        for rule in sorted(self._rule_table.remote_rule_info):
            info = self._rule_table.remote_rule_info[rule]
            # If the rule is not staged, do not get stats on it
            if not info['Staged']:
                continue

            self._staging_stats[rule] = StagingStatistic(
                info['StagedAt'],
                info['StagedUntil'],
                self._current_time,
                rule
            )

        return len(self._staging_stats) != 0

    def _update_alert_count(self):
        """Transform Athena query results into alert counts for rules_engine

        Args:
            query (str): Athena query to run and wait for results

        Returns:
            dict: Representation of alert counts, where key is the rule name
                and value is the alert count (int) since this rule was staged
        """
        query = StagingStatistic.construct_compound_count_query(self._staging_stats.values())
        LOGGER.debug('Running compound query for alert count: \'%s\'', query)

        for results in self._athena_client.query_result_paginator(query):
            for i, row in enumerate(results['ResultSet']['Rows']):
                if i == 0: # skip header row
                    continue

                row_values = [data.values()[0] for data in row['Data']]
                rule_name, alert_count = row_values[0], int(row_values[1])

                self._staging_stats[rule_name].alert_count = alert_count

    def run(self):
        """Perform statistic analysis of currently staged rules"""
        if not self._get_staging_info():
            LOGGER.debug('No staged rules to promote')
            return

        self._update_alert_count()

        self._promote_rules()

        self._publisher.publish(self._staging_stats.values())

    def _promote_rules(self):
        """Promote any rule that has not resulted in any alerts since being staged"""
        for rule in self._rules_to_be_promoted:
            LOGGER.info('Promoting rule \'%s\' at %s', rule, self._current_time)
            self._rule_table.toggle_staged_state(rule, False)

    @property
    def _rules_to_be_promoted(self):
        """Returns a list of rules that are eligible for promotion"""
        return [rule for rule, stat in self._staging_stats.iteritems()
                if self._current_time > stat.staged_until and stat.alert_count == 0]

    @property
    def _rules_failing_promotion(self):
        """Returns a list of rules that are ineligible for promotion"""
        return [rule for rule, stat in self._staging_stats.iteritems()
                if stat.alert_count != 0]
