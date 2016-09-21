from peewee import IntegerField, CharField, TextField, ForeignKeyField, DecimalField, DateTimeField
import peewee
import playhouse.signals
import time
import simplejson
import binascii
import datetime
import re
import sys, os
sys.path.append( os.path.join( os.path.dirname(__file__), '..' ) )
sys.path.append( os.path.join( os.path.dirname(__file__), '..' , 'lib' ) )
import config
import misc
import dashd
from misc import printdbg

# our mixin
from governance_class import GovernanceClass

db = config.db

try:
    db.connect()
except peewee.OperationalError as e:
    print "%s" % e
    print "Please ensure MySQL database service is running and user access is properly configured in 'config.py'"
    sys.exit(2)


# TODO: put this in a lookup table
DASHD_GOVOBJ_TYPES = {
    'proposal': 1,
    'superblock': 2,
}

# === models ===

class BaseModel(playhouse.signals.Model):

    class Meta:
        database = db

    @classmethod
    def is_database_connected(self):
        return not db.is_closed()

class GovernanceObject(BaseModel):
    parent_id = IntegerField(default=0)
    object_creation_time = IntegerField(default=int(time.time()))
    object_hash = CharField(max_length=64)
    object_parent_hash = CharField(default='0')
    object_type = IntegerField(default=0)
    object_revision = IntegerField(default=1)
    object_fee_tx = CharField(default='')
    yes_count = IntegerField(default=0)
    no_count = IntegerField(default=0)
    abstain_count = IntegerField(default=0)
    absolute_yes_count = IntegerField(default=0)

    class Meta:
        db_table = 'governance_objects'

    # sync dashd gobject list with our local relational DB backend
    @classmethod
    def sync(self, dashd):
        golist = dashd.rpc_command('gobject', 'list')

        # objects which are removed from the network should be removed from the DB
        for purged in self.purged_network_objects(golist.keys()):
            # SOMEDAY: possible archive step here
            purged.delete_instance(recursive=True, delete_nullable=True)

        for item in golist.values():
            (go, subobj) = self.import_gobject_from_dashd(dashd, item)

    @classmethod
    def purged_network_objects(self, network_object_hashes):
        return self.select().where(~(self.object_hash << network_object_hashes))

    @classmethod
    def import_gobject_from_dashd(self, dashd, rec):
        import dashlib
        import inflection

        object_hex = rec['DataHex']
        object_hash = rec['Hash']

        gobj_dict = {
            'object_hash': object_hash,
            'object_fee_tx': rec['CollateralHash'],
            'absolute_yes_count': rec['AbsoluteYesCount'],
            'abstain_count': rec['AbstainCount'],
            'yes_count': rec['YesCount'],
            'no_count': rec['NoCount'],
        }

        # shim/dashd conversion
        object_hex = dashlib.SHIM_deserialise_from_dashd(object_hex)
        objects = dashlib.deserialise(object_hex)
        subobj = None

        obj_type, dikt = objects[0:2:1]
        obj_type = inflection.pluralize(obj_type)
        subclass = self._meta.reverse_rel[obj_type].model_class

        # set object_type in govobj table
        gobj_dict['object_type'] = subclass.govobj_type

        # exclude any invalid model data from dashd...
        valid_keys = subclass.serialisable_fields()
        subdikt = { k: dikt[k] for k in valid_keys if k in dikt }

        # get/create, then sync vote counts from dashd, with every run
        govobj, created = self.get_or_create(object_hash=object_hash, defaults=gobj_dict)
        if created:
            printdbg("govobj created = %s" % created)
        count = govobj.update(**gobj_dict).where(self.id == govobj.id).execute()
        if count:
            printdbg("govobj updated = %d" % count)
        subdikt['governance_object'] = govobj

        # get/create, then sync payment amounts, etc. from dashd - Dashd is the master
        try:
            subobj, created = subclass.get_or_create(object_hash=object_hash, defaults=subdikt)
        except (peewee.OperationalError, peewee.IntegrityError) as e:
            # in this case, vote as delete, and log the vote in the DB
            printdbg("Got invalid object from dashd! %s" % e)
            if not govobj.voted_on(signal=VoteSignals.delete, outcome=VoteOutcomes.yes):
                govobj.vote(dashd, VoteSignals.delete, VoteOutcomes.yes)
            return (govobj, None)

        if created:
            printdbg("subobj created = %s" % created)
        count = subobj.update(**subdikt).where(subclass.id == subobj.id).execute()
        if count:
            printdbg("subobj updated = %d" % count)

        # ATM, returns a tuple w/gov attributes and the govobj
        return (govobj, subobj)

    def get_vote_command(self, signal, outcome):
        cmd = [ 'gobject', 'vote-conf', self.object_hash,
                signal.name, outcome.name ]
        return cmd

    def vote(self, dashd, signal, outcome):
        import dashlib

        # At this point, will probably never reach here. But doesn't hurt to
        # have an extra check just in case objects get out of sync (people will
        # muck with the DB).
        if ( self.object_hash == '0' or not misc.is_hash(self.object_hash)):
            printdbg("No governance object hash, nothing to vote on.")
            return

        # TODO: ensure Signal, Outcome are valid options for dashd
        vote_command = self.get_vote_command(signal, outcome)
        printdbg(' '.join(vote_command))
        output = dashd.rpc_command(*vote_command)

        # extract vote output parsing to external lib
        voted = dashlib.did_we_vote(output)

        if voted:
            # TODO: ensure signal, outcome exist in lookup table or raise exception
            v = Vote(
                governance_object=self,
                signal=signal,
                outcome=outcome,
                object_hash=self.object_hash,
            )
            v.save()

    def voted_on(self, **kwargs):
        signal  = kwargs.get('signal', None)
        outcome = kwargs.get('outcome', None)

        query = self.votes

        if signal:
            query = query.where(Vote.signal == signal)

        if outcome:
            query = query.where(Vote.outcome == outcome)

        count = query.count()
        return count

class Setting(BaseModel):
    name     = CharField(default='')
    value    = CharField(default='')
    created_at = DateTimeField(default=datetime.datetime.utcnow())
    updated_at = DateTimeField(default=datetime.datetime.utcnow())

    class Meta:
        db_table = 'settings'

class Proposal(GovernanceClass, BaseModel):
    governance_object = ForeignKeyField(GovernanceObject, related_name = 'proposals')
    name = CharField(default='', max_length=20)
    url = CharField(default='')
    start_epoch = IntegerField()
    end_epoch = IntegerField()
    payment_address = CharField(max_length=36)
    payment_amount = DecimalField(max_digits=16, decimal_places=8)
    object_hash = CharField(max_length=64)

    # TODO: remove this redundancy if/when dashd can be fixed to use
    # strings/types instead of ENUM types for type ID
    # govobj_type = 1
    govobj_type = DASHD_GOVOBJ_TYPES['proposal']

    class Meta:
        db_table = 'proposals'

    def is_valid(self, dashd):
        import dashlib
        now = misc.get_epoch()

        # proposal name is normalized (something like "[a-zA-Z0-9-_]+")
        if not re.match( '^[-_a-zA-Z0-9]+$', self.name ):
            return False

        # end date < start date
        if ( self.end_epoch <= self.start_epoch ):
            return False

        # end date < current date
        if ( self.end_epoch <= now ):
            return False

        # budget check
        max_budget = dashd.next_superblock_max_budget()
        if ( max_budget and (self.payment_amount > max_budget) ):
            return False

        # amount can't be negative or 0
        if ( self.payment_amount <= 0 ):
            return False

        # payment address is valid base58 dash addr, non-multisig
        if not dashlib.is_valid_dash_address( self.payment_address, config.network ):
            return False

        # URL
        if (len(self.url.strip()) < 4):
            return False

        return True

    def is_deletable(self):
        # end_date < (current_date - 30 days)
        thirty_days = (86400 * 30)
        if ( self.end_epoch < (misc.get_epoch() - thirty_days) ):
            return True

        # TBD (item moved to external storage/DashDrive, etc.)
        return False

    @classmethod
    def approved_and_ranked(self, dashd):
        proposal_quorum = dashd.governance_quorum()
        next_superblock_max_budget = dashd.next_superblock_max_budget()

        # return all approved proposals, in order of descending vote count
        #
        # we need a secondary 'order by' in case of a tie on vote count, since
        # superblocks must be deterministic
        query = (self
                 .select(self, GovernanceObject)  # Note that we are selecting both models.
                 .join(GovernanceObject)
                 .where(GovernanceObject.absolute_yes_count > proposal_quorum)
                 .order_by(GovernanceObject.absolute_yes_count.desc(), GovernanceObject.object_hash)
                 )

        ranked = []
        for proposal in query:
            proposal.max_budget = next_superblock_max_budget
            if proposal.is_valid(dashd):
                ranked.append(proposal)

        return ranked

    @property
    def rank(self):
        rank = 0
        if self.governance_object:
            rank = self.governance_object.absolute_yes_count
            return rank

class Superblock(BaseModel, GovernanceClass):
    governance_object = ForeignKeyField(GovernanceObject, related_name = 'superblocks')
    event_block_height   = IntegerField()
    payment_addresses    = TextField()
    payment_amounts      = TextField()
    sb_hash      = CharField()
    object_hash = CharField(max_length=64)

    # TODO: remove this redundancy if/when dashd can be fixed to use
    # strings/types instead of ENUM types for type ID
    # govobj_type = 2
    govobj_type = DASHD_GOVOBJ_TYPES['superblock']

    class Meta:
        db_table = 'superblocks'

    # superblocks don't have a collateral tx to submit
    def get_submit_command(self):
        import dashlib
        obj_data = dashlib.SHIM_serialise_for_dashd(self.serialise())

        # new superblocks won't have parent_hash, revision, etc...
        cmd = ['gobject', 'submit', '0', '1', str(int(time.time())), obj_data]

        return cmd

    def is_valid(self, dashd):
        # ensure EBH is on-cycle
        if (self.event_block_height != dashd.next_superblock_height()):
            return False

        return True

    def is_deletable(self):
        # end_date < (current_date - 30 days)
        # TBD (item moved to external storage/DashDrive, etc.)
        pass

    def hash(self):
        import dashlib
        return dashlib.hashit(self.serialise())

    def hex_hash(self):
        return "%x" % self.hash()

    # workaround for now, b/c we must uniquely ID a superblock with the hash,
    # in case of differing superblocks
    #
    # this prevents sb_hash from being added to the serialised fields
    @classmethod
    def serialisable_fields(self):
        return ['event_block_height', 'payment_addresses', 'payment_amounts']

    # has this masternode voted to fund *any* superblocks at the given
    # event_block_height?
    @classmethod
    def is_voted_funding(self, ebh):
        count = (self.select()
                    .where(self.event_block_height == ebh)
                    .join(GovernanceObject)
                    .join(Vote)
                    .join(Signal)
                    .switch(Vote) # switch join query context back to Vote
                    .join(Outcome)
                    .where(Vote.signal == VoteSignals.funding)
                    .where(Vote.outcome == VoteOutcomes.yes)
                .count())
        return count

    @classmethod
    def latest(self):
        try:
            obj = self.select().order_by(self.event_block_height).desc().limit(1)[0]
        except IndexError as e:
            obj = None
        return obj

    @classmethod
    def at_height(self, ebh):
        query = (self.select().where(self.event_block_height == ebh))
        return query

    @classmethod
    def find_highest_deterministic(self, sb_hash):
        # highest block hash wins
        query = (self.select()
                    .where(self.sb_hash == sb_hash)
                    .order_by(self.object_hash.desc()))
        try:
            obj = query.limit(1)[0]
        except IndexError as e:
            obj = None
        return obj

# ok, this is an awkward way to implement these...
# "hook" into the Superblock model and run this code just before any save()
from playhouse.signals import pre_save
@pre_save(sender=Superblock)
def on_save_handler(model_class, instance, created):
    instance.sb_hash = instance.hex_hash()

class Signal(BaseModel):
    name = CharField(unique=True)
    created_at = DateTimeField(default=datetime.datetime.utcnow())
    updated_at = DateTimeField(default=datetime.datetime.utcnow())
    class Meta:
        db_table = 'signals'

class Outcome(BaseModel):
    name = CharField(unique=True)
    created_at = DateTimeField(default=datetime.datetime.utcnow())
    updated_at = DateTimeField(default=datetime.datetime.utcnow())
    class Meta:
        db_table = 'outcomes'

class Vote(BaseModel):
    governance_object = ForeignKeyField(GovernanceObject, related_name = 'votes')
    signal = ForeignKeyField(Signal, related_name = 'votes')
    outcome = ForeignKeyField(Outcome, related_name = 'votes')
    voted_at = DateTimeField(default=datetime.datetime.utcnow())
    created_at = DateTimeField(default=datetime.datetime.utcnow())
    updated_at = DateTimeField(default=datetime.datetime.utcnow())
    object_hash = CharField(max_length=64)

    class Meta:
        db_table = 'votes'

# === /models ===

def load_db_seeds():
    rows_created = 0

    for name in ['funding', 'valid', 'delete']:
        (obj, created) = Signal.get_or_create(name=name)
        if created:
            rows_created = rows_created + 1

    for name in ['yes', 'no', 'abstain']:
        (obj, created) = Outcome.get_or_create(name=name)
        if created:
            rows_created = rows_created + 1

    return rows_created

def check_db_sane():
    missing_table_models = []

    for model in [ GovernanceObject, Setting, Proposal, Superblock, Signal, Outcome, Vote ]:
        if not getattr(model, 'table_exists')():
            missing_table_models.append(model)
            print "[warning]: table for %s (%s) doesn't exist in DB." % (model, model._meta.db_table)

    if missing_table_models:
        print "[warning]: Missing database tables. Auto-creating tables."
        try:
            db.create_tables(missing_table_models, safe=True)
        except peewee.OperationalError as e:
            print "[error] Could not create tables: %s" % e

# sanity checks...
check_db_sane()     # ensure tables exist
load_db_seeds()     # ensure seed data loaded

# convenience accessors
VoteSignals = misc.Bunch(**{ sig.name: sig for sig in Signal.select() })
VoteOutcomes = misc.Bunch(**{ out.name: out for out in Outcome.select() })
