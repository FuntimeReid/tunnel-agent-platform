package nlp

import (
	"context"
	"errors"
	"reflect"
	"testing"

	"ragflow/internal/dao"
	"ragflow/internal/entity"

	"gorm.io/gorm"
)

type accessLevelUserLookup struct {
	user *entity.User
	err  error
}

func (f accessLevelUserLookup) GetByTenantID(context.Context, *gorm.DB, string) (*entity.User, error) {
	if f.user != nil && f.user.IsSuperuser != nil && *f.user.IsSuperuser {
		user := *f.user
		user.AccessLevel = entity.MaxAccessLevel
		return &user, f.err
	}
	return f.user, f.err
}

type accessLevelDocument struct {
	id    string
	kbID  string
	level int
}

type accessLevelDocumentLookup struct {
	documents []accessLevelDocument
	err       error
}

func configureAccessLevelForTest(service *RetrievalService, callerID string, level int, docs ...accessLevelDocument) {
	superuser := false
	service.userAccessDAO = accessLevelUserLookup{user: &entity.User{ID: callerID, AccessLevel: level, IsSuperuser: &superuser}}
	service.documentAccessDAO = accessLevelDocumentLookup{documents: docs}
}

func (f accessLevelDocumentLookup) ListIDsByKBIDsAtOrBelowAccessLevel(_ context.Context, _ *gorm.DB, kbIDs []string, level int) ([]string, error) {
	if f.err != nil {
		return nil, f.err
	}
	kbSet := make(map[string]struct{}, len(kbIDs))
	for _, kbID := range kbIDs {
		kbSet[kbID] = struct{}{}
	}
	var ids []string
	for _, doc := range f.documents {
		if _, ok := kbSet[doc.kbID]; ok && doc.level <= level {
			ids = append(ids, doc.id)
		}
	}
	return ids, nil
}

func (f accessLevelDocumentLookup) HasDocumentAboveAccessLevel(_ context.Context, _ *gorm.DB, kbIDs []string, level int) (bool, error) {
	if f.err != nil {
		return false, f.err
	}
	kbSet := make(map[string]struct{}, len(kbIDs))
	for _, kbID := range kbIDs {
		kbSet[kbID] = struct{}{}
	}
	for _, doc := range f.documents {
		if _, ok := kbSet[doc.kbID]; ok && doc.level > level {
			return true, nil
		}
	}
	return false, nil
}

func TestResolveAccessibleDocumentIDsUsesAuthenticatedLevel(t *testing.T) {
	documents := []accessLevelDocument{
		{id: "public", kbID: "kb-1", level: 0},
		{id: "internal", kbID: "kb-1", level: 1},
		{id: "confidential", kbID: "kb-1", level: 2},
		{id: "restricted", kbID: "kb-1", level: 3},
		{id: "other-kb", kbID: "kb-2", level: 0},
	}
	tests := []struct {
		name       string
		level      int
		superuser  bool
		want       []string
	}{
		{name: "default historical document level", level: 0, want: []string{"public"}},
		{name: "level one", level: 1, want: []string{"internal", "public"}},
		{name: "level two", level: 2, want: []string{"confidential", "internal", "public"}},
		{name: "level three", level: 3, want: []string{"confidential", "internal", "public", "restricted"}},
		{name: "superuser always has level three", level: 0, superuser: true, want: []string{"confidential", "internal", "public", "restricted"}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			service := &RetrievalService{
				userAccessDAO: accessLevelUserLookup{user: &entity.User{
					ID:          "caller-1",
					AccessLevel: test.level,
					IsSuperuser: &test.superuser,
				}},
				documentAccessDAO: accessLevelDocumentLookup{documents: documents},
			}
			got, err := service.resolveAccessibleDocumentIDs(t.Context(), "caller-1", []string{"kb-1"})
			if err != nil {
				t.Fatalf("resolveAccessibleDocumentIDs: %v", err)
			}
			if !reflect.DeepEqual(got, test.want) {
				t.Fatalf("accessible IDs = %v, want %v", got, test.want)
			}
		})
	}
}

func TestResolveAccessibleDocumentIDsFailsClosed(t *testing.T) {
	lookupErr := errors.New("database unavailable")
	tests := []struct {
		name       string
		callerID   string
		userLookup userAccessLookup
		docLookup  documentAccessLookup
	}{
		{name: "missing authenticated caller", callerID: "", userLookup: accessLevelUserLookup{user: &entity.User{}}},
		{name: "user lookup error", callerID: "user-1", userLookup: accessLevelUserLookup{err: lookupErr}},
		{name: "document lookup error", callerID: "user-1", userLookup: accessLevelUserLookup{user: &entity.User{AccessLevel: 0}}, docLookup: accessLevelDocumentLookup{err: lookupErr}},
		{name: "invalid user level", callerID: "user-1", userLookup: accessLevelUserLookup{user: &entity.User{AccessLevel: 4}}, docLookup: accessLevelDocumentLookup{}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			service := &RetrievalService{userAccessDAO: test.userLookup, documentAccessDAO: test.docLookup}
			if ids, err := service.resolveAccessibleDocumentIDs(t.Context(), test.callerID, []string{"kb-1"}); err == nil || ids != nil {
				t.Fatalf("resolveAccessibleDocumentIDs = %v, %v; want error and no IDs", ids, err)
			}
		})
	}
}

func TestIntersectAccessDocumentIDsCannotWidenScope(t *testing.T) {
	accessible := []string{"doc-public", "doc-secret"}
	tests := []struct {
		name      string
		requested []string
		want      []string
	}{
		{name: "no caller scope means all accessible", requested: nil, want: accessible},
		{name: "request scope intersects accessible IDs", requested: []string{"doc-secret", "doc-forbidden", "doc-secret"}, want: []string{"doc-secret"}},
		{name: "explicit empty scope stays empty", requested: []string{}, want: []string{}},
		{name: "metadata no-match sentinel stays empty", requested: []string{"-999"}, want: []string{}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := intersectAccessDocumentIDs(test.requested, accessible); !reflect.DeepEqual(got, test.want) {
				t.Fatalf("intersection = %v, want %v", got, test.want)
			}
		})
	}
}

func TestCanUseKnowledgeGraphRequiresCompleteDocumentAccess(t *testing.T) {
	for _, test := range []struct {
		name      string
		level     int
		superuser bool
		docs      []accessLevelDocument
		want      bool
	}{
		{name: "regular user with public docs only", level: 0, docs: []accessLevelDocument{{id: "public", kbID: "kb-1", level: 0}}, want: true},
		{name: "restricted document disables aggregated graph", level: 0, docs: []accessLevelDocument{{id: "secret", kbID: "kb-1", level: 2}}, want: false},
		{name: "highest level can use graph", level: 3, docs: []accessLevelDocument{{id: "secret", kbID: "kb-1", level: 3}}, want: true},
		{name: "superuser is highest", level: 0, superuser: true, docs: []accessLevelDocument{{id: "secret", kbID: "kb-1", level: 3}}, want: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			isSuperuser := test.superuser
			user := &entity.User{ID: "caller-1", AccessLevel: test.level, IsSuperuser: &isSuperuser}
			if test.superuser {
				user.AccessLevel = entity.MaxAccessLevel
			}
			allowed, err := canUseKnowledgeGraphForUser(t.Context(), user, []string{"kb-1"}, accessLevelDocumentLookup{documents: test.docs})
			if err != nil {
				t.Fatalf("canUseKnowledgeGraphForUser: %v", err)
			}
			if allowed != test.want {
				t.Fatalf("graph permission = %v, want %v", allowed, test.want)
			}
		})
	}
}

func TestResolveAccessibleDocumentIDsRequiresDatasetScope(t *testing.T) {
	service := &RetrievalService{
		userAccessDAO:     accessLevelUserLookup{user: &entity.User{AccessLevel: 0}},
		documentAccessDAO: accessLevelDocumentLookup{},
	}
	if _, err := service.resolveAccessibleDocumentIDs(t.Context(), "caller-1", nil); err == nil {
		t.Fatal("expected empty dataset scope to fail")
	}
}

func TestRetrievalRestrictsCandidateSearchBeforeRerank(t *testing.T) {
	oldQueryBuilder := globalQueryBuilder
	globalQueryBuilder = NewQueryBuilder()
	defer func() { globalQueryBuilder = oldQueryBuilder }()

	engine := &retrievalCountEngine{rows: []map[string]interface{}{{
		"id":                  "chunk-1",
		"content_ltks":        "alpha",
		"content_with_weight": "alpha",
		"_score":              0.9,
	}}}
	service := NewRetrievalService(engine, &dao.DocumentDAO{})
	configureAccessLevelForTest(service, "caller-1", 0,
		accessLevelDocument{id: "public-doc", kbID: "kb-1", level: 0},
		accessLevelDocument{id: "secret-doc", kbID: "kb-1", level: 3},
	)
	result, err := service.Retrieval(t.Context(), &RetrievalRequest{
		CallerID: "caller-1",
		Question: "alpha",
		KbIDs:    []string{"kb-1"},
		DocIDs:   []string{"public-doc", "secret-doc"},
		Filter:   map[string]interface{}{"doc_id": []string{"secret-doc"}},
		Page:     1,
		PageSize: 5,
	})
	if err != nil {
		t.Fatalf("Retrieval: %v", err)
	}
	if len(result.Chunks) != 1 {
		t.Fatalf("chunks = %d, want one authorized chunk", len(result.Chunks))
	}
	if len(engine.searchFilters) == 0 {
		t.Fatal("retrieval did not call the candidate search engine")
	}
	docIDs, ok := engine.searchFilters[0]["doc_id"].([]string)
	if !ok || !reflect.DeepEqual(docIDs, []string{"public-doc"}) {
		t.Fatalf("candidate doc_id filter = %#v, want only public-doc", engine.searchFilters[0]["doc_id"])
	}
}

func TestRetrievalRejectsMissingCallerBeforeSearch(t *testing.T) {
	engine := &retrievalCountEngine{}
	service := NewRetrievalService(engine, &dao.DocumentDAO{})
	_, err := service.Retrieval(t.Context(), &RetrievalRequest{
		Question: "alpha",
		KbIDs:    []string{"kb-1"},
		Page:     1,
		PageSize: 5,
	})
	if err == nil {
		t.Fatal("expected retrieval without authenticated caller to fail")
	}
	if len(engine.searchFilters) != 0 {
		t.Fatal("retrieval search ran without an authenticated caller")
	}
}