package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"ragflow/internal/entity"
)

func TestAccessLevelAdminEndpointRejectsRegularUser(t *testing.T) {
	gin.SetMode(gin.TestMode)
	regular := false
	user := &entity.User{ID: "user-1", IsSuperuser: &regular}
	request := httptest.NewRequest(http.MethodPut, "/api/v1/access-level/documents/doc-1", strings.NewReader(`{"access_level":3}`))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	context, _ := gin.CreateTestContext(recorder)
	context.Request = request
	context.Params = gin.Params{{Key: "document_id", Value: "doc-1"}}
	context.Set("user", user)

	NewAccessLevelHandler().SetDocumentAccessLevel(context)

	if recorder.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want forbidden: %s", recorder.Code, recorder.Body.String())
	}
}

func TestAccessLevelAdminEndpointDoesNotTrustBodyRole(t *testing.T) {
	gin.SetMode(gin.TestMode)
	regular := false
	request := httptest.NewRequest(http.MethodPut, "/api/v1/access-level/documents/doc-1", strings.NewReader(`{"access_level":3,"is_superuser":true,"role":"admin"}`))
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	context, _ := gin.CreateTestContext(recorder)
	context.Request = request
	context.Params = gin.Params{{Key: "document_id", Value: "doc-1"}}
	context.Set("user", &entity.User{ID: "user-1", IsSuperuser: &regular})

	NewAccessLevelHandler().SetDocumentAccessLevel(context)

	if recorder.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want forbidden: %s", recorder.Code, recorder.Body.String())
	}
}
