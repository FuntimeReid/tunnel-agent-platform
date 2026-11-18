package dao

import (
	"testing"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"

	"ragflow/internal/entity"
)

func setupUserAccessLevelTestDB(t *testing.T) *gorm.DB {
	t.Helper()
	db, err := gorm.Open(sqlite.Open(":memory:"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	if err := db.AutoMigrate(&entity.User{}); err != nil {
		t.Fatalf("migrate user: %v", err)
	}
	return db
}

func TestUserDAOGetByTenantIDPromotesSuperuserToMaximumAccess(t *testing.T) {
	db := setupUserAccessLevelTestDB(t)
	superuser := true
	if err := db.Create(&entity.User{ID: "admin", Email: "admin@example.com", Nickname: "admin", IsSuperuser: &superuser, AccessLevel: 0}).Error; err != nil {
		t.Fatalf("create user: %v", err)
	}

	user, err := NewUserDAO().GetByTenantID(t.Context(), db, "admin")
	if err != nil {
		t.Fatalf("GetByTenantID: %v", err)
	}
	if user.AccessLevel != entity.MaxAccessLevel {
		t.Fatalf("superuser level = %d, want %d", user.AccessLevel, entity.MaxAccessLevel)
	}
}

func TestUserDAOUpdateAccessLevelValidatesRangeAndSuperuserInvariant(t *testing.T) {
	db := setupUserAccessLevelTestDB(t)
	regular := false
	superuser := true
	for _, user := range []*entity.User{
		{ID: "regular", Email: "regular@example.com", Nickname: "regular", IsSuperuser: &regular},
		{ID: "admin", Email: "admin@example.com", Nickname: "admin", IsSuperuser: &superuser},
	} {
		if err := db.Create(user).Error; err != nil {
			t.Fatalf("create user %s: %v", user.ID, err)
		}
	}

	da := NewUserDAO()
	if err := da.UpdateAccessLevel(t.Context(), db, "regular", 2); err != nil {
		t.Fatalf("update regular level: %v", err)
	}
	if err := da.UpdateAccessLevel(t.Context(), db, "regular", 4); err == nil {
		t.Fatal("expected out-of-range level to be rejected")
	}
	if err := da.UpdateAccessLevel(t.Context(), db, "admin", 2); err == nil {
		t.Fatal("expected superuser demotion to be rejected")
	}
	if err := da.UpdateAccessLevel(t.Context(), db, "admin", entity.MaxAccessLevel); err != nil {
		t.Fatalf("setting superuser to maximum: %v", err)
	}
}