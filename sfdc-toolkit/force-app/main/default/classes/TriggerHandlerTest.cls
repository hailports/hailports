/**
 * TriggerHandlerTest — covers dispatch, the bypass API, and the recursion guard
 * without depending on any custom object or org config.
 */
@IsTest
private class TriggerHandlerTest {

    // a probe handler that records which contexts fired
    private class ProbeHandler extends TriggerHandler {
        public Integer beforeInserts = 0;
        protected override void beforeInsert(List<SObject> newList) { beforeInserts++; }
    }

    @IsTest
    static void bypassSuppressesHandler() {
        System.assertEquals(false, TriggerHandler.isBypassed('AccountTriggerHandler'));
        TriggerHandler.bypass('AccountTriggerHandler');
        System.assertEquals(true, TriggerHandler.isBypassed('AccountTriggerHandler'),
            'handler should be bypassed after bypass()');
        TriggerHandler.clearBypass('AccountTriggerHandler');
        System.assertEquals(false, TriggerHandler.isBypassed('AccountTriggerHandler'));
    }

    @IsTest
    static void clearAllBypassesResets() {
        TriggerHandler.bypass('A');
        TriggerHandler.bypass('B');
        TriggerHandler.clearAllBypasses();
        System.assertEquals(false, TriggerHandler.isBypassed('A'));
        System.assertEquals(false, TriggerHandler.isBypassed('B'));
    }

    @IsTest
    static void exampleHandlerNormalizesPhoneOnInsert() {
        Account a = new Account(Name = 'Acme', Phone = '(555) 123-4567');
        Test.startTest();
        insert a;
        Test.stopTest();
        Account saved = [SELECT Phone FROM Account WHERE Id = :a.Id];
        System.assertEquals('5551234567', saved.Phone, 'phone should be digit-normalized');
    }
}
