from twisted.python import log
from twisted.python.log import logging
from twisted.internet.defer import inlineCallbacks, returnValue
from django.contrib.auth.models import User

from vumi.service import Worker, Consumer, Publisher
from vumi.webapp.api import utils

import json
import time
from datetime import datetime

from vumi.webapp.api import models


class SMSKeywordConsumer(Consumer):
    exchange_name = "vumi"
    exchange_type = "direct"
    durable = True
    delivery_mode = 2
    queue_name = "" # overwritten by subclass
    routing_key = "" # overwritten by subclass


    def consume_json(self, dictionary):
        message = dictionary.get('short_message')
        head = message.split(' ')[0]
        try:
            user = User.objects.get(username=head)
            
            received_sms = models.ReceivedSMS()
            received_sms.user = user
            received_sms.to_msisdn = dictionary.get('destination_addr')
            received_sms.from_msisdn = dictionary.get('source_addr')
            received_sms.message = dictionary.get('short_message')
            
            # FIXME: this is hacky
            received_sms.transport_name = self.queue_name.split('.')[-1]
            # FIXME: EsmeTransceiver doesn't publish these over JSON / AMQP
            # received_sms.transport_msg_id = ...
            # FIXME: this isn't accurate, we might receive it much earlier than
            #        we save it because it could be queued / backlogged.
            received_sms.received_at = datetime.now()
            # FIXME: this is where the fun begins, guessing charsets.
            # received_sms.charset = ...
            received_sms.save()
            
            profile = user.get_profile()
            urlcallback_set = profile.urlcallback_set.filter(name='sms_received')
            for urlcallback in urlcallback_set:
                try:
                    url = urlcallback.url
                    log.msg('URL: %s' % urlcallback.url)
                    params = [
                            ("callback_name", "sms_received"),
                            ("to_msisdn", str(dictionary.get('destination_addr'))),
                            ("from_msisdn", str(dictionary.get('source_addr'))),
                            ("message", str(dictionary.get('short_message')))
                            ]
                    url, resp = utils.callback(url, params)
                    log.msg('RESP: %s' % resp)
                except Exception, e:
                    log.err(e)
            
        except User.DoesNotExist:
            log.msg("Couldn't find user for message: %s" % message)
        log.msg("DELIVER SM %s consumed by %s" % (json.dumps(dictionary),self.__class__.__name__))


def dynamically_create_keyword_consumer(name,**kwargs):
    return type("%s_SMSKeywordConsumer" % name, (SMSKeywordConsumer,), kwargs)


class SMSKeywordWorker(Worker):
    """
    A worker that fires off URLCallback's for incoming SMSs
    with keywords
    """

    @inlineCallbacks
    def startWorker(self):
        log.msg("Starting the SMSKeywordWorkers for: %s" % self.config.get('OPERATOR_NUMBER'))
        upstream = self.config.get('UPSTREAM')
        for network,msisdn in self.config.get('OPERATOR_NUMBER').items():
            if len(msisdn):
                yield self.start_consumer(dynamically_create_keyword_consumer(network,
                    routing_key='sms.inbound.%s.%s' % (upstream, msisdn),
                    queue_name='sms.inbound.%s.%s' % (upstream, msisdn)
                ))

    def stopWorker(self):
        log.msg("Stopping the SMSKeywordWorker")


#==================================================================================================

class SMSReceiptConsumer(Consumer):
    exchange_name = "vumi"
    exchange_type = "direct"
    durable = True
    delivery_mode = 2
    queue_name = "" # overwritten by subclass
    routing_key = "" # overwritten by subclass


    def consume_json(self, dictionary):
        _id = dictionary['delivery_report']['id']
        if len(_id):
            resp = models.SMPPResp.objects.get(message_id=_id)
            sent = resp.sent_sms
            sent.transport_status = dictionary['delivery_report']['stat']
            sent.delivered_at = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.strptime(
                        "20"+dictionary['delivery_report']['done_date'],
                        "%Y%m%d%H%M%S"))
            sent.save()
            user = sent.user
            profile = user.get_profile()
            urlcallback_set = profile.urlcallback_set.filter(name='sms_receipt')
            for urlcallback in urlcallback_set:
                try:
                    url = urlcallback.url
                    log.msg('URL: %s' % urlcallback.url)
                    params = [
                            ("callback_name", "sms_receipt"),
                            ("id", sent.id),
                            ("transport_status", dictionary['delivery_report']['stat']),
                            ("transport_status_display", dictionary['delivery_report']['stat']),
                            ("created_at", sent.created_at),
                            ("updated_at", sent.updated_at),
                            ("delivered_at", time.strftime(
                                    "%Y-%m-%d %H:%M:%S",
                                    time.strptime(
                                        "20"+dictionary['delivery_report']['done_date'],
                                        "%Y%m%d%H%M%S"
                                        )
                                    )),
                            ("from_msisdn", dictionary['destination_addr']),
                            ("to_msisdn", sent.to_msisdn),
                            ("message", sent.message),
                            ]
                    url, resp = utils.callback(url, [(p[0],str(p[1])) for p in params])
                    log.msg('RESP: %s' % resp)
                except Exception, e:
                    log.err(e)
        log.msg("RECEIPT SM %s consumed by %s" % (json.dumps(dictionary),self.__class__.__name__))


#class FallbackSMSReceiptConsumer(SMSReceiptConsumer):
    #routing_key = 'receipt.fallback'


def dynamically_create_receipt_consumer(name,**kwargs):
    return type("%s_SMSReceiptConsumer" % name, (SMSReceiptConsumer,), kwargs)


class SMSReceiptWorker(Worker):
    """
    A worker that fires off URLCallback's for incoming Receipts
    """

    @inlineCallbacks
    def startWorker(self):
        log.msg("Starting the SMSReceiptWorkers for: %s" % self.config.get('OPERATOR_NUMBER'))
        upstream = self.config.get('UPSTREAM', '')
        yield self.start_consumer(dynamically_create_receipt_consumer(upstream,
                    routing_key='sms.receipt.%s' % upstream,
                    queue_name='sms.receipt.%s' % upstream
                ))
        #yield self.start_consumer(FallbackSMSReceiptConsumer)

    def stopWorker(self):
        log.msg("Stopping the SMSReceiptWorker")


#==================================================================================================

class SMSBatchConsumer(Consumer):
    exchange_name = "vumi"
    exchange_type = "direct"
    durable = True
    delivery_mode = 2
    queue_name = "sms_send"
    # FIXME: topical routing key for direct exchange type? 
    routing_key = "vumi.webapp.sms.send"

    def __init__(self, publisher):
        self.publisher = publisher

    def consume_json(self, dictionary):
        log.msg("SM BATCH %s consumed by %s" % (json.dumps(dictionary),self.__class__.__name__))
        payload = []
        kwargs = dictionary.get('kwargs')
        if kwargs:
            pk = kwargs.get('pk')
            for o in models.SentSMS.objects.filter(batch=pk):
                mess = {
                        'transport_name':o.transport_name,
                        'batch':o.batch_id,
                        'from_msisdn':o.from_msisdn,
                        'user':o.user_id,
                        'to_msisdn':o.to_msisdn,
                        'message':o.message,
                        'id':o.id
                        }
                print ">>>>", json.dumps(mess)
                self.publisher.publish_json(mess)
                #reactor.callLater(0, self.publisher.publish_json, mess)
        return True

    def consume(self, message):
        if self.consume_json(json.loads(message.content.body)):
            self.ack(message)


#class FallbackSMSBatchConsumer(SMSBatchConsumer):
    #routing_key = 'batch.fallback'


class IndivPublisher(Publisher):
    """
    This publisher publishes all incoming SMPP messages to the
    `vumi.smpp` exchange, its default routing key is `smpp.fallback`
    """
    exchange_name = "vumi"
    exchange_type = "direct"
    routing_key = "sms.outbound.fallback"
    durable = True
    auto_delete = False
    delivery_mode = 2

    def publish_json(self, dictionary, **kwargs):
        transport = str(dictionary.get('transport_name', 'fallback')).lower()
        routing_key = 'sms.outbound.' + transport
        kwargs.update({'routing_key':routing_key})
        log.msg("Publishing JSON %s with extra args: %s" % (dictionary, kwargs))
        super(IndivPublisher, self).publish_json(dictionary, **kwargs)


class SMSBatchWorker(Worker):
    """
    A worker that breaks up batches of sms's into individual sms's
    """

    @inlineCallbacks
    def startWorker(self):
        log.msg("Starting the SMSBatchWorker")
        self.publisher = yield self.start_publisher(IndivPublisher)
        yield self.start_consumer(SMSBatchConsumer, self.publisher)
        #yield self.start_consumer(FallbackSMSBatchConsumer)

    def stopWorker(self):
        log.msg("Stopping the SMSBatchWorker")

