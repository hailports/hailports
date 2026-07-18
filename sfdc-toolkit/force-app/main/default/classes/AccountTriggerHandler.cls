/**
 * AccountTriggerHandler — worked example of extending TriggerHandler.
 * Normalizes a phone field and stamps a rollup on insert/update. Generic.
 */
public class AccountTriggerHandler extends TriggerHandler {

    protected override void beforeInsert(List<SObject> newList) {
        normalize((List<Account>) newList);
    }

    protected override void beforeUpdate(Map<Id, SObject> oldMap, Map<Id, SObject> newMap) {
        normalize((List<Account>) newMap.values());
    }

    private void normalize(List<Account> accounts) {
        for (Account a : accounts) {
            if (String.isNotBlank(a.Phone)) {
                a.Phone = a.Phone.replaceAll('[^0-9+]', '');
            }
        }
    }
}
