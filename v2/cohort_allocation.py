"""Bind P5 intents to the already registered canonical P3 cohort accounts.

The standalone P5 adapter carves isolated-test account names. A full cohort has
already carved P1's fixed pools; it must reuse those accounts rather than double
allocate the parent. This bridge confers no worker or request admission itself.
"""
from dataclasses import replace
import json
from .allocation import require
from .allocation_services import AccountingBridge


class CohortAccountingBridge(AccountingBridge):
    def __init__(self, allocation, accounting):
        super().__init__(allocation, accounting)
        with accounting.transaction() as db:
            row=db.execute("SELECT value FROM settings WHERE key='cohort'").fetchone()
            require(row is not None,'cohort-unregistered')
            self.registration=json.loads(row[0])
            require(db.execute("SELECT 1 FROM settings WHERE key='profile-configured'").fetchone() is not None,
                    'cohort-profile-unconfigured')
            cfg=allocation.config
            require(cfg['arm_id']==cfg['strategy'] and cfg['profile']==self.registration['profile']
                    and cfg['problem_id']==self.registration['problem']
                    and cfg['contracts_sha256']==self.registration['contracts_sha256'], 'cohort-allocation-identity')
            row=db.execute('SELECT config FROM arms WHERE id=?',(self.arm,)).fetchone()
            require(row is not None,'cohort-arm-missing')
            arm=json.loads(row[0])
            require(int(arm['start']*1000)==cfg['started_at_ms']
                    and int(arm['deadline']*1000)==cfg['deadline_ms'], 'cohort-original-clock')

    def pool_id(self,pool):
        return self.arm+'/'+pool.replace(':','/')

    def register_pools(self):
        # configure_cohort is the sole pool allocator in this composition.
        policy=self.allocation.policy
        pools=(dict(policy['pools_microusd']) if self.arm=='dsf-swarm' else
               {model+':'+pool:cap for model,values in policy['model_pools_microusd'].items()
                for pool,cap in values.items() if cap})
        if self.arm=='mixed-swarm' and policy['recovery_microusd']:
            pools['recovery']=policy['recovery_microusd']
        with self.accounting.transaction() as db:
            for pool,cap in pools.items():
                if not cap:continue
                row=db.execute('SELECT cap,arm FROM accounts WHERE id=?',(self.pool_id(pool),)).fetchone()
                require(row is not None and row['cap']==cap and row['arm']==self.arm,'cohort-pool-mismatch')
        return True

    def context(self,intent_id,*,credential_id):
        context=super().context(intent_id,credential_id=credential_id)
        item=self.provision(intent_id)
        role=context.role
        return replace(context,cohort=self.registration['id'],problem=self.registration['problem'],
            worker=item['worker'],ticket=intent_id,phase=item['pool'],
            request_class='peer-review' if role=='review' else 'specialist' if role=='specialist' else 'primary')
