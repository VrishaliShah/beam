#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Google Cloud PubSub sources and sinks.

Cloud Pub/Sub sources and sinks are currently supported only in streaming
pipelines, during remote execution.

This API is currently under development and is subject to change.
"""

from __future__ import absolute_import

import re
from builtins import object
from typing import Any
from typing import Optional

from future.utils import iteritems
from past.builtins import unicode

from apache_beam import coders
from apache_beam.io.iobase import Read
from apache_beam.io.iobase import Write
from apache_beam.runners.dataflow.native_io import iobase as dataflow_io
from apache_beam.transforms import Map
from apache_beam.transforms import PTransform
from apache_beam.transforms.display import DisplayDataItem
from apache_beam.utils.annotations import deprecated

try:
  from google.cloud import pubsub
except ImportError:
  pubsub = None

__all__ = ['PubsubMessage', 'ReadFromPubSub', 'ReadStringsFromPubSub',
           'WriteStringsToPubSub', 'WriteToPubSub']


class PubsubMessage(object):
  """Represents a Cloud Pub/Sub message.

  Message payload includes the data and attributes fields. For the payload to be
  valid, at least one of its fields must be non-empty.

  This interface is experimental. No backwards compatibility guarantees.

  Attributes:
    data: (bytes) Message data. May be None.
    attributes: (dict) Key-value map of str to str, containing both user-defined
      and service generated attributes (such as id_label and
      timestamp_attribute). May be None.
  """

  def __init__(self, data, attributes):
    if data is None and not attributes:
      raise ValueError('Either data (%r) or attributes (%r) must be set.',
                       data, attributes)
    self.data = data
    self.attributes = attributes

  def __hash__(self):
    return hash((self.data, frozenset(self.attributes.items())))

  def __eq__(self, other):
    return isinstance(other, PubsubMessage) and (
        self.data == other.data and
        self.attributes == other.attributes)

  def __ne__(self, other):
    # TODO(BEAM-5949): Needed for Python 2 compatibility.
    return not self == other

  def __repr__(self):
    return 'PubsubMessage(%s, %s)' % (self.data, self.attributes)

  @staticmethod
  def _from_proto_str(proto_msg):
    # type: (bytes) -> PubsubMessage
    """Construct from serialized form of ``PubsubMessage``.

    Args:
      proto_msg: String containing a serialized protobuf of type
      https://cloud.google.com/pubsub/docs/reference/rpc/google.pubsub.v1#google.pubsub.v1.PubsubMessage

    Returns:
      A new PubsubMessage object.
    """
    msg = pubsub.types.pubsub_pb2.PubsubMessage()
    msg.ParseFromString(proto_msg)
    # Convert ScalarMapContainer to dict.
    attributes = dict((key, msg.attributes[key]) for key in msg.attributes)
    return PubsubMessage(msg.data, attributes)

  def _to_proto_str(self):
    """Get serialized form of ``PubsubMessage``.

    Args:
      proto_msg: str containing a serialized protobuf.

    Returns:
      A str containing a serialized protobuf of type
      https://cloud.google.com/pubsub/docs/reference/rpc/google.pubsub.v1#google.pubsub.v1.PubsubMessage
      containing the payload of this object.
    """
    msg = pubsub.types.pubsub_pb2.PubsubMessage()
    msg.data = self.data
    for key, value in iteritems(self.attributes):
      msg.attributes[key] = value
    return msg.SerializeToString()

  @staticmethod
  def _from_message(msg):
    # type: (Any) -> PubsubMessage
    """Construct from ``google.cloud.pubsub_v1.subscriber.message.Message``.

    https://googleapis.github.io/google-cloud-python/latest/pubsub/subscriber/api/message.html
    """
    # Convert ScalarMapContainer to dict.
    attributes = dict((key, msg.attributes[key]) for key in msg.attributes)
    return PubsubMessage(msg.data, attributes)


class ReadFromPubSub(PTransform):
  """A ``PTransform`` for reading from Cloud Pub/Sub."""
  # Implementation note: This ``PTransform`` is overridden by Directrunner.

  def __init__(self,
               topic=None,  # type: Optional[str]
               subscription=None,  # type: Optional[str]
               id_label=None,  # type: Optional[str]
               with_attributes=False,  # type: bool
               timestamp_attribute=None  # type: Optional[str]
              ):
    # type: (...) -> None
    """Initializes ``ReadFromPubSub``.

    Args:
      topic: Cloud Pub/Sub topic in the form
        "projects/<project>/topics/<topic>". If provided, subscription must be
        None.
      subscription: Existing Cloud Pub/Sub subscription to use in the
        form "projects/<project>/subscriptions/<subscription>". If not
        specified, a temporary subscription will be created from the specified
        topic. If provided, topic must be None.
      id_label: The attribute on incoming Pub/Sub messages to use as a unique
        record identifier. When specified, the value of this attribute (which
        can be any string that uniquely identifies the record) will be used for
        deduplication of messages. If not provided, we cannot guarantee
        that no duplicate data will be delivered on the Pub/Sub stream. In this
        case, deduplication of the stream will be strictly best effort.
      with_attributes:
        True - output elements will be :class:`~PubsubMessage` objects.
        False - output elements will be of type ``bytes`` (message
        data only).
      timestamp_attribute: Message value to use as element timestamp. If None,
        uses message publishing time as the timestamp.

        Timestamp values should be in one of two formats:

        - A numerical value representing the number of milliseconds since the
          Unix epoch.
        - A string in RFC 3339 format, UTC timezone. Example:
          ``2015-10-29T23:41:41.123Z``. The sub-second component of the
          timestamp is optional, and digits beyond the first three (i.e., time
          units smaller than milliseconds) may be ignored.
    """
    super(ReadFromPubSub, self).__init__()
    self.with_attributes = with_attributes
    self._source = _PubSubSource(
        topic=topic,
        subscription=subscription,
        id_label=id_label,
        with_attributes=self.with_attributes,
        timestamp_attribute=timestamp_attribute)

  def expand(self, pvalue):
    pcoll = pvalue.pipeline | Read(self._source)
    pcoll.element_type = bytes
    if self.with_attributes:
      pcoll = pcoll | Map(PubsubMessage._from_proto_str)
      pcoll.element_type = PubsubMessage
    return pcoll

  def to_runner_api_parameter(self, context):
    # Required as this is identified by type in PTransformOverrides.
    # TODO(BEAM-3812): Use an actual URN here.
    return self.to_runner_api_pickled(context)


@deprecated(since='2.7.0', extra_message='Use ReadFromPubSub instead.')
def ReadStringsFromPubSub(topic=None, subscription=None, id_label=None):
  return _ReadStringsFromPubSub(topic, subscription, id_label)


class _ReadStringsFromPubSub(PTransform):
  """This class is deprecated. Use ``ReadFromPubSub`` instead."""

  def __init__(self, topic=None, subscription=None, id_label=None):
    super(_ReadStringsFromPubSub, self).__init__()
    self.topic = topic
    self.subscription = subscription
    self.id_label = id_label

  def expand(self, pvalue):
    p = (pvalue.pipeline
         | ReadFromPubSub(self.topic, self.subscription, self.id_label,
                          with_attributes=False)
         | 'DecodeString' >> Map(lambda b: b.decode('utf-8')))
    p.element_type = unicode
    return p


@deprecated(since='2.7.0', extra_message='Use WriteToPubSub instead.')
def WriteStringsToPubSub(topic):
  return _WriteStringsToPubSub(topic)


class _WriteStringsToPubSub(PTransform):
  """This class is deprecated. Use ``WriteToPubSub`` instead."""

  def __init__(self, topic):
    """Initializes ``_WriteStringsToPubSub``.

    Attributes:
      topic: Cloud Pub/Sub topic in the form "/topics/<project>/<topic>".
    """
    super(_WriteStringsToPubSub, self).__init__()
    self._sink = _PubSubSink(topic, id_label=None, with_attributes=False,
                             timestamp_attribute=None)

  def expand(self, pcoll):
    pcoll = pcoll | 'EncodeString' >> Map(lambda s: s.encode('utf-8'))
    pcoll.element_type = bytes
    return pcoll | Write(self._sink)


class WriteToPubSub(PTransform):
  """A ``PTransform`` for writing messages to Cloud Pub/Sub."""
  # Implementation note: This ``PTransform`` is overridden by Directrunner.

  def __init__(self,
               topic,  # type: str
               with_attributes=False,  # type: bool
               id_label=None,  # type: Optional[str]
               timestamp_attribute=None  # type: Optional[str]
              ):
    # type: (...) -> None
    """Initializes ``WriteToPubSub``.

    Args:
      topic: Cloud Pub/Sub topic in the form "/topics/<project>/<topic>".
      with_attributes:
        True - input elements will be :class:`~PubsubMessage` objects.
        False - input elements will be of type ``bytes`` (message
        data only).
      id_label: If set, will set an attribute for each Cloud Pub/Sub message
        with the given name and a unique value. This attribute can then be used
        in a ReadFromPubSub PTransform to deduplicate messages.
      timestamp_attribute: If set, will set an attribute for each Cloud Pub/Sub
        message with the given name and the message's publish time as the value.
    """
    super(WriteToPubSub, self).__init__()
    self.with_attributes = with_attributes
    self.id_label = id_label
    self.timestamp_attribute = timestamp_attribute
    self._sink = _PubSubSink(topic, id_label, with_attributes,
                             timestamp_attribute)

  @staticmethod
  def to_proto_str(element):
    # type: (PubsubMessage) -> bytes
    if not isinstance(element, PubsubMessage):
      raise TypeError('Unexpected element. Type: %s (expected: PubsubMessage), '
                      'value: %r' % (type(element), element))
    return element._to_proto_str()

  def expand(self, pcoll):
    if self.with_attributes:
      pcoll = pcoll | 'ToProtobuf' >> Map(self.to_proto_str)

    # Without attributes, message data is written as-is. With attributes,
    # message data + attributes are passed as a serialized protobuf string (see
    # ``PubsubMessage._to_proto_str`` for exact protobuf message type).
    pcoll.element_type = bytes
    return pcoll | Write(self._sink)

  def to_runner_api_parameter(self, context):
    # Required as this is identified by type in PTransformOverrides.
    # TODO(BEAM-3812): Use an actual URN here.
    return self.to_runner_api_pickled(context)


PROJECT_ID_REGEXP = '[a-z][-a-z0-9:.]{4,61}[a-z0-9]'
SUBSCRIPTION_REGEXP = 'projects/([^/]+)/subscriptions/(.+)'
TOPIC_REGEXP = 'projects/([^/]+)/topics/(.+)'


def parse_topic(full_topic):
  match = re.match(TOPIC_REGEXP, full_topic)
  if not match:
    raise ValueError(
        'PubSub topic must be in the form "projects/<project>/topics'
        '/<topic>" (got %r).' % full_topic)
  project, topic_name = match.group(1), match.group(2)
  if not re.match(PROJECT_ID_REGEXP, project):
    raise ValueError('Invalid PubSub project name: %r.' % project)
  return project, topic_name


def parse_subscription(full_subscription):
  match = re.match(SUBSCRIPTION_REGEXP, full_subscription)
  if not match:
    raise ValueError(
        'PubSub subscription must be in the form "projects/<project>'
        '/subscriptions/<subscription>" (got %r).' % full_subscription)
  project, subscription_name = match.group(1), match.group(2)
  if not re.match(PROJECT_ID_REGEXP, project):
    raise ValueError('Invalid PubSub project name: %r.' % project)
  return project, subscription_name


class _PubSubSource(dataflow_io.NativeSource):
  """Source for a Cloud Pub/Sub topic or subscription.

  This ``NativeSource`` is overridden by a native Pubsub implementation.

  Attributes:
    with_attributes: If False, will fetch just message data. Otherwise,
      fetches ``PubsubMessage`` protobufs.
  """

  def __init__(self,
               topic=None,  # type: Optional[str]
               subscription=None,  # type: Optional[str]
               id_label=None,  # type: Optional[str]
               with_attributes=False,  # type: bool
               timestamp_attribute=None  # type: Optional[str]
              ):
    self.coder = coders.BytesCoder()
    self.full_topic = topic
    self.full_subscription = subscription
    self.topic_name = None
    self.subscription_name = None
    self.id_label = id_label
    self.with_attributes = with_attributes
    self.timestamp_attribute = timestamp_attribute

    # Perform some validation on the topic and subscription.
    if not (topic or subscription):
      raise ValueError('Either a topic or subscription must be provided.')
    if topic and subscription:
      raise ValueError('Only one of topic or subscription should be provided.')

    if topic:
      self.project, self.topic_name = parse_topic(topic)
    if subscription:
      self.project, self.subscription_name = parse_subscription(subscription)

  @property
  def format(self):
    """Source format name required for remote execution."""
    return 'pubsub'

  def display_data(self):
    return {'id_label':
            DisplayDataItem(self.id_label,
                            label='ID Label Attribute').drop_if_none(),
            'topic':
            DisplayDataItem(self.full_topic,
                            label='Pubsub Topic').drop_if_none(),
            'subscription':
            DisplayDataItem(self.full_subscription,
                            label='Pubsub Subscription').drop_if_none(),
            'with_attributes':
            DisplayDataItem(self.with_attributes,
                            label='With Attributes').drop_if_none(),
            'timestamp_attribute':
            DisplayDataItem(self.timestamp_attribute,
                            label='Timestamp Attribute').drop_if_none(),
           }

  def reader(self):
    raise NotImplementedError

  def is_bounded(self):
    return False


class _PubSubSink(dataflow_io.NativeSink):
  """Sink for a Cloud Pub/Sub topic.

  This ``NativeSource`` is overridden by a native Pubsub implementation.
  """

  def __init__(self,
               topic,  # type: str
               id_label,  # type: Optional[str]
               with_attributes,  # type: bool
               timestamp_attribute  # type: Optional[str]
              ):
    self.coder = coders.BytesCoder()
    self.full_topic = topic
    self.id_label = id_label
    self.with_attributes = with_attributes
    self.timestamp_attribute = timestamp_attribute

    self.project, self.topic_name = parse_topic(topic)

  @property
  def format(self):
    """Sink format name required for remote execution."""
    return 'pubsub'

  def display_data(self):
    return {
        'topic': DisplayDataItem(self.full_topic, label='Pubsub Topic'),
        'id_label': DisplayDataItem(self.id_label, label='ID Label Attribute'),
        'with_attributes': DisplayDataItem(
            self.with_attributes, label='With Attributes').drop_if_none(),
        'timestamp_attribute': DisplayDataItem(
            self.timestamp_attribute, label='Timestamp Attribute'),
    }

  def writer(self):
    raise NotImplementedError
