/**
 * TriggerHandler — a lightweight virtual base for one-handler-per-object trigger logic.
 *
 * Extend it, override only the contexts you need, and invoke `run()` from a
 * one-line trigger. Provides a recursion guard and a runtime bypass API so data
 * loads and tests can suppress specific handlers deterministically.
 *
 * Generic reference implementation — no org-specific logic.
 */
public virtual class TriggerHandler {

    // handlers suppressed at runtime, by class name
    private static Set<String> bypassed = new Set<String>();
    // per-handler recursion counters, keyed by class+context
    private static Map<String, Integer> runs = new Map<String, Integer>();

    private final String handlerName;
    // max re-entrant runs per context before we stop (guards infinite recursion)
    protected Integer maxLoops = 5;

    public TriggerHandler() {
        this.handlerName = String.valueOf(this).substring(0, String.valueOf(this).indexOf(':'));
    }

    /** Dispatch to the right context method. Call this from the trigger. */
    public void run() {
        if (!Trigger.isExecuting || bypassed.contains(handlerName)) {
            return;
        }
        if (!incrementAndCheck()) {
            return;
        }
        if (Trigger.isBefore) {
            if (Trigger.isInsert)      beforeInsert(Trigger.new);
            else if (Trigger.isUpdate) beforeUpdate(Trigger.oldMap, Trigger.newMap);
            else if (Trigger.isDelete) beforeDelete(Trigger.oldMap);
        } else {
            if (Trigger.isInsert)      afterInsert(Trigger.newMap);
            else if (Trigger.isUpdate) afterUpdate(Trigger.oldMap, Trigger.newMap);
            else if (Trigger.isDelete) afterDelete(Trigger.oldMap);
            else if (Trigger.isUndelete) afterUndelete(Trigger.newMap);
        }
    }

    private Boolean incrementAndCheck() {
        String key = handlerName + ':' + triggerContext();
        Integer n = runs.containsKey(key) ? runs.get(key) : 0;
        if (n >= maxLoops) {
            return false;
        }
        runs.put(key, n + 1);
        return true;
    }

    private String triggerContext() {
        return (Trigger.isBefore ? 'before_' : 'after_') + Trigger.operationType;
    }

    // ── runtime bypass API ──────────────────────────────────────────────
    public static void bypass(String handlerName) { bypassed.add(handlerName); }
    public static void clearBypass(String handlerName) { bypassed.remove(handlerName); }
    public static Boolean isBypassed(String handlerName) { return bypassed.contains(handlerName); }
    public static void clearAllBypasses() { bypassed.clear(); }

    // ── override the contexts you need ──────────────────────────────────
    protected virtual void beforeInsert(List<SObject> newList) {}
    protected virtual void beforeUpdate(Map<Id, SObject> oldMap, Map<Id, SObject> newMap) {}
    protected virtual void beforeDelete(Map<Id, SObject> oldMap) {}
    protected virtual void afterInsert(Map<Id, SObject> newMap) {}
    protected virtual void afterUpdate(Map<Id, SObject> oldMap, Map<Id, SObject> newMap) {}
    protected virtual void afterDelete(Map<Id, SObject> oldMap) {}
    protected virtual void afterUndelete(Map<Id, SObject> newMap) {}
}
