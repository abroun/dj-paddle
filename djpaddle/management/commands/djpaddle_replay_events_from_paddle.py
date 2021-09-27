"""
replay_events_from_paddle command.
"""
import copy
from datetime import datetime, timezone, timedelta
from django.core.management.base import BaseCommand
from django.db.models import Max
import logging
import sys

from ... import settings, signals
from ...models import paddle_client, convert_string_to_datetime, ReplayedEvent

log = logging.getLogger(__name__)

ALERTS_PER_PAGE = 50

def get_event_payload_list(start_time, end_time):

    # Paddle uses 1-based indexing for pages, so start at page 1 and continue until
    # we get an empty response
    page = 1
    event_payload_list = []
    while True:
        api_response = paddle_client.get_webhook_history(
            page=page, alerts_per_page=ALERTS_PER_PAGE, 
            query_tail=start_time, query_head=end_time)

        event_list = api_response.get("data", [])
        if len(event_list) == 0:
            break

        for event in event_list:

            payload = event["fields"]
            
            # Put alert_name into the payload to match webhook payloads
            payload["alert_name"] = event["alert_name"]
            event_payload_list.append(payload)

        page += 1

    # Sort the events from earliest to latest time
    event_payload_list = sorted(event_payload_list, key=lambda x: convert_string_to_datetime(x["event_time"]))

    return event_payload_list

class Command(BaseCommand):
    """Sync plans from Paddle."""

    help = "Pull new events (not already pulled) from Paddle and replay them."

    def handle(self, *args, **options):
        """Pull events from Paddle and replay."""

        # Work out start and end time of the query. We use DJPADDLE_REPLAYED_EVENT_RETENTION_DAYS
        # to limit the amount of data we could pull from the API
        start_time = ReplayedEvent.objects.aggregate(Max("time"))["time__max"]   # Gives None if ReplayedEvents is empty
        if start_time is not None:
            start_time += timedelta(seconds=1)

        end_time = datetime.now(timezone.utc) - timedelta(minutes=1)

        earliest_start_time = end_time - timedelta(days=settings.DJPADDLE_REPLAYED_EVENT_RETENTION_DAYS)
        if start_time is None or start_time < earliest_start_time:
            start_time = earliest_start_time

        # Clean out old events
        ReplayedEvent.objects.filter(time__lt=earliest_start_time).delete()

        # Get list of events and replay
        event_payload_list = get_event_payload_list(start_time, end_time)
        num_replayed_events = 0
        for payload in event_payload_list:

            alert_name = payload["alert_name"]

            signal = getattr(signals, alert_name)
            if signal:  # pragma: no cover

                try:
                    log.info("Replaying " + alert_name)
                    sent_payload = copy.deepcopy(payload)   # Copy payload as it can be modified by signal handler
                    signal.send(sender=self.__class__, payload=sent_payload)
                    num_replayed_events += 1
                except Exception as e:
                    log.error("Got exception replaying")
                    log.error(str(payload) + "\n")
                    log.error(str(e) + "\n")
                    log.error("Replay failed. Exiting")
                    sys.exit(-1)

            event_time = payload["event_time"]
            event = ReplayedEvent(time=convert_string_to_datetime(event_time), payload=payload)
            event.save()

        log.info(f"Replayed {num_replayed_events} events")
