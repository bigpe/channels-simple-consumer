"""
Websocket: Simple Consumer
====================================
Simple websocket consumer
"""

from __future__ import annotations
import uuid
from dataclasses import dataclass
from inspect import isclass
from typing import Callable as Cl, Any

from asgiref.sync import async_to_sync
from channels.consumer import get_handler_name
from channels.db import database_sync_to_async
from channels.generic.websocket import JsonWebsocketConsumer
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from django.core.cache import cache

from .decoratos import auth, safe
from .signatures import ResponsePayload, Payload, Event, TargetsEnum, Message, EventSystem, \
    MessageSystem, TargetResolver, LookupUser
from .utils import camel_to_snake, user_cache_key, camel_to_dot

User: AbstractUser = get_user_model()


class SimpleEvent:
    request_payload_type = Payload  #: Payload type for request this name from client side
    response_payload_type = Payload  #: Payload type for both (who send/and who must receive)
    response_payload_type_initiator = Payload  #: Payload type for users who send this name from client side
    response_payload_type_target = Payload  #: Payload type for users who access to receive this name
    target = TargetsEnum.for_all  #: Who must receive this name
    consumer = None  #: Consumer object instance
    hidden = False  #: If hidden, event can't be called from client side

    def before_catch(self, message: Message, payload: request_payload_type):
        """
        Do something once before wrap to initiator or targets user catch blocks
        Use if you need mutate DB or any different data
        """
        ...

    def initiator_catch(self, message: Message, payload: request_payload_type):
        """
        Catch this block if fired event's initiator is being found
        You can do any staff at this place, also you can return new event, it will be fired to initiator
        """
        ...

    def target_catch(self, message: Message, payload: request_payload_type):
        """
        Catch this block if fired event's target is being found
        You can do any staff at this place, also you can return new event, it will be fired to target
        """
        ...

    def after_catch(self, message: Message, payload: request_payload_type):
        """
        Do something once after wrap to initiator or targets user catch blocks
        Use if you need mutate DB or any different data
        """

    def __init__(self, consumer: SimpleConsumer, content=None, payload: Payload = None):
        self.consumer: SimpleConsumer = consumer if consumer else self.consumer
        if not self.consumer:
            print('Provide consumer instance for direct call event')
            return

        payload = payload if payload else Payload()
        self.payload = payload
        self.content = self.parse_content(content=content, payload=payload)

        # If name marked as Hidden, send action not exist
        if self.hidden:
            self.consumer.Error(payload=ResponsePayload.ActionNotExist(), consumer=self.consumer).fire()
            return

    def return_event(self, payload: [Payload, dict] = None) -> Event:
        payload = payload if payload else self.payload
        content = self.content
        return Event(name=content.pop('type'), system=content.pop('system'), payload=payload)

    def fire_client(self):
        self.consumer.send_broadcast(
            self.content,
            do_for_target=self.target_catch,
            do_for_initiator=self.initiator_catch,
            target=self.target,
            do_before=self.before_catch,
            payload_type=self.request_payload_type
        )

    def fire(self, payload: [Payload, dict] = None):
        self.consumer.send_json(self.return_event(payload).serialize())

    def fire_broadcast(self, payload: [Payload, dict] = None):
        event: Event = self.return_event(payload=payload)
        self.consumer.send_to_group(event)

    def parse_content(self, content: dict, payload: [Payload, dict]):
        event_name = camel_to_dot(self.__class__.__name__)

        # If name data not provided, it means name instance was called directly, you can provide payload
        # and consumer instance to imitate client side communicate,
        # name type (name) and system data obtained automatically
        if not content:
            self.hidden = False
            content = {
                'type': event_name,
                'payload': payload.serialize() if isinstance(payload, Payload) else payload,
                'system': self.consumer.get_systems().serialize()
            }
        return content


class SimpleConsumer(JsonWebsocketConsumer):
    broadcast_group = None  #: Group to join after connect
    authed = False  #: Check connected user is authed, if not - close connect
    custom_target_resolver = {}  #: If you need define rules for lookup users who want to receive events (target)

    def __init__(self):
        super(SimpleConsumer, self).__init__()
        self.hide_events()

    def __call__(self, scope, receive, send):
        self.inject_user(scope)
        return super(SimpleConsumer, self).__call__(scope, receive, send)

    @staticmethod
    def inject_user(scope):
        if not scope.get('user', False):
            scope['user'] = AnonymousUser()

    @auth
    def connect(self):
        self.cache_system()
        self.join_group(self.broadcast_group)
        self.after_connect()

    def after_connect(self):
        ...

    def before_disconnect(self):
        ...

    def disconnect(self, code):
        self.before_disconnect()

    def send_json(self, content, close=False):
        if 'system' in content:
            content.pop('system')
        super(SimpleConsumer, self).send_json(content, close)

    def cache_system(self):
        if not self.get_user().is_anonymous:
            cache.set(user_cache_key(self.get_user()), self.get_systems().serialize(), 40 * 60)

    def get_user(self, user_id: int = None) -> User:
        return User.objects.get(id=user_id) if user_id else self.scope.get('user', AnonymousUser())

    def join_group(self, group_name: str):
        if group_name:
            async_to_sync(self.channel_layer.group_add)(group_name, self.channel_name)

    def leave_group(self, group_name: str):
        if group_name:
            async_to_sync(self.channel_layer.group_discard)(group_name, self.channel_name)

    def get_systems(self) -> EventSystem:
        return EventSystem(
            initiator_channel=self.channel_name,
            initiator_user_id=self.scope['user'].id,
            event_id=str(uuid.uuid4())
        )

    @safe
    def receive(self, *arg, **kwargs):
        super().receive(*arg, **kwargs)

    @safe
    def send(self, *arg, **kwargs):
        super().send(*arg, **kwargs)

    @database_sync_to_async
    def dispatch(self, content):
        handler: Cl = getattr(self, get_handler_name(content), None)
        if isclass(handler):
            handler: Any
            if issubclass(handler, SimpleEvent):
                handler(consumer=self, content=content).fire_client()
        else:
            handler(content)

    def receive_json(self, content: dict, **kwargs):
        if not self.channel_layer:
            self.Error(payload=ResponsePayload.ChannelLayerDisabled(), consumer=self).fire()
            return
        content.update({'name': content.pop('event')})
        event, error = self.check_signature(lambda: Event(**content, system=self.get_systems()))
        event: Event
        if error:
            return
        if event:
            action_handler = getattr(self, get_handler_name(event.to_channels()), None)
            if action_handler:
                action_handler.consumer = self
            if not action_handler:
                self.Error(payload=ResponsePayload.ActionNotExist(), consumer=self).fire()
                return
        if self.broadcast_group:
            self.send_to_group(event)
        else:
            print(f'Broadcast group not specified for {self.__class__.__name__}, broadcast not sent')

    def send_to_group(self, event: Event, group_name: str = None):
        async_to_sync(
            self.channel_layer.group_send
        )(self.broadcast_group if not group_name else group_name, event.to_channels())

    def check_signature(self, f: Cl):
        error = False
        data = None
        try:
            data = f()
        except TypeError as e:
            if ' missing ' in str(e):
                required = str(e).split('argument: ')[1].strip().replace("'", '')
                self.Error(payload=ResponsePayload.PayloadSignatureWrong(required=required), consumer=self).fire()
                error = True
            if ' unexpected ' in str(e):
                unexpected = str(e).split('argument')[1].strip().replace("'", '')
                self.Error(payload=ResponsePayload.ActionSignatureWrong(unexpected=unexpected), consumer=self).fire()
                error = True
        return data, error

    def parse_payload(self, content, payload_type: Payload()):
        payload = Payload(**content['payload'])
        error = False
        if payload_type:
            payload, error = self.check_signature(lambda: payload_type(**payload.serialize()))
        return payload, error

    def parse_message(self, target: TargetsEnum, payload: Payload, content: dict):
        # TODO kwargs lookup for to_user_id/to_username
        TargetResolver.update(self.custom_target_resolver)
        message = Message(
            payload=payload,
            system=MessageSystem(
                **EventSystem(**content['system']).serialize(),
                receiver_channel=self.channel_name
            ),
            user=self.scope['user'],
            target=target,
            target_resolver=TargetResolver,
            lookup=LookupUser(**{f'to_{field}': getattr(payload, f'to_{field}', None) for field in LookupUser.Fields})
        )
        return message

    @safe
    def send_broadcast(self, content, target, do_for_target: Cl = None, do_for_initiator: Cl = None,
                       do_before: Cl = None, payload_type=None):

        payload, error = self.parse_payload(content, payload_type)
        payload: Payload
        if error:
            return

        message = self.parse_message(target, payload, content)

        if (message.target == TargetsEnum.for_user and not message.target_user) and message.is_initiator:
            self.Error(payload=ResponsePayload.RecipientNotExist(), consumer=self).fire()
            return  # Interrupt action for initiator and action for target if recipient not found

        def before():
            if do_before and not message.before_activated:
                do_before(message, payload)
                message.before_activate()
                return True
            return False

        def do_for(do: Cl):
            activated = before()
            event: [Event, dict] = do()
            if event:
                self.send_json(content=event.serialize() if isinstance(event, Event) else event)
            if message.before_activated and not activated:
                message.before_drop()

        if message.is_initiator and do_for_initiator:
            do_for(lambda: do_for_initiator(message, payload))

        if message.is_target and do_for_target:
            do_for(lambda: do_for_target(message, payload))

    def hide_events(self):
        attributes = list(filter(lambda attr: not attr.startswith('_') and not attr.startswith('__'), dir(self)))
        classes = list(filter(lambda cls: hasattr(getattr(self, cls), '__base__'), attributes))
        events = list(filter(lambda e: issubclass(getattr(self, e), SimpleEvent), classes))
        for event in events:
            event_class = getattr(self, event)
            hidden = getattr(event_class, 'hidden', False)
            if not hidden:
                setattr(self, camel_to_snake(event), event_class)

    class Error(SimpleEvent):
        """Error event"""
        request_payload_type = None
        hidden = True
